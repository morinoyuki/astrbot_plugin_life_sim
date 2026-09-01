"""向量记忆存储 —— 记录「发生过的事情」(剧情 / 事件记忆)。

与既有 lore(角色设定 / 世界观设定)解耦:lore 仍全部注入 system prompt,
本模块只负责**剧情事件记忆**的写入、语义检索与召回,解决长期会话中早期记忆
因上下文压缩而丢失的问题。

设计要点:
- **生命周期 = 当前会话**:每个 scope(group/user)一个记忆库文件,随 `/删除`
  或 `/创建` 覆盖而 `delete_scope` 清空,不跨会话保留。
- **检索结果注入 user 消息而非 system prompt**:system prompt 必须字节级稳定
  才能命中前缀缓存;检索到的记忆每轮都不同,放进每轮都在变的 user_input 里,
  零额外缓存成本(与 `_build_narrative_ref_tag` 同一思路)。
- **向量检索轻量自容**:默认用 numpy 暴力余弦相似度(每会话记忆量小,足够快),
  不硬依赖 faiss。若 AstrBot 配置了 Embedding Provider,优先用它做语义嵌入;
  否则回退到本地稳定哈希 n-gram 嵌入(跨重启稳定,零下载)。
- **线程安全**:读写走 `asyncio.to_thread`,内部用 `threading.RLock` 保护
  FAISS 式的「检查相似 → 写入」原子性。

存储布局::
    <data_dir>/vector_memory/<scope>.json
    {
      "dim": 256,
      "source": "local",
      "entries": [
        {"id": "...", "content": "...", "turn": 12, "importance": 1,
         "created_at": "...", "metadata": {...}, "vector": [...]}, ...
      ]
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any

try:
    import numpy as np
except Exception:  # 极低概率:环境无 numpy 时降级到纯文本召回(仅按 recency)
    np = None  # type: ignore

from .storage_base import ensure_dir, sanitize_key, write_json_atomic

SUB_DIR = "vector_memory"
LOCAL_DIM = 256  # 本地哈希嵌入的固定维度


def _stable_hash(s: str) -> int:
    """跨进程 / 跨重启稳定的哈希(不用内置 hash,其受 PYTHONHASHSEED 扰动)。"""
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


def _local_embed(texts: list[str]) -> list[list[float]]:
    """本地 n-gram 特征哈希嵌入(零依赖回退)。

    按字符 uni/bi/tri-gram 哈希进固定维度桶,带符号累加后 L2 归一化。
    对中文共享 n-gram 的同义 / 相关片段能给出可用的相似度,跨重启稳定。
    """
    if np is None:
        # 无 numpy:退化为单维 one-hot-ish(效果差但不会崩)
        return [[1.0 for _ in texts] for _ in range(1)]
    dim = LOCAL_DIM
    out: list[list[float]] = []
    for t in texts:
        vec = np.zeros(dim, dtype=np.float32)
        s = t.lower()
        n = len(s)
        grams: list[str] = list(s)
        for i in range(n - 1):
            grams.append(s[i : i + 2])
        for i in range(n - 2):
            grams.append(s[i : i + 3])
        for g in grams:
            h = _stable_hash(g)
            idx = h % dim
            sign = 1.0 if (h & 1) else -1.0
            vec[idx] += sign
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        out.append(vec.tolist())
    return out


class MemoryStore:
    """按 scope 分库的向量记忆存储。

    用法::
        store = MemoryStore(data_dir)
        store.set_embedding_provider(prov)          # 可选:接入 AstrBot Embedding Provider
        await store.add(scope, content, turn=1)     # 写入一条记忆
        hits = await store.search(scope, query)     # 语义召回
        await store.delete_scope(scope)             # 删除会话时清理
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._root = ensure_dir(os.path.join(data_dir, SUB_DIR))
        # 内存缓存:{scope: (mtime, {entry})} — 避免每轮读写磁盘
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.RLock()
        self._embedding_provider: Any = None
        self._embed_source = "local"
        self._dim = LOCAL_DIM

    # ─── 嵌入源配置 ───────────────────────────────────────────

    def set_embedding_provider(self, provider: Any) -> None:
        """接入 AstrBot Embedding Provider(若可用)以提升语义质量。

        provider 需暴露 ``get_embeddings`` / ``get_dim``。不可用时保持本地回退。
        """
        try:
            if provider is None:
                return
            has_emb = hasattr(provider, "get_embeddings")
            has_dim = hasattr(provider, "get_dim")
            if not has_emb:
                return
            self._embedding_provider = provider
            self._embed_source = "provider"
            dim = 0
            if has_dim:
                try:
                    dim = int(provider.get_dim())
                except Exception:
                    dim = 0
            if dim and dim > 0:
                self._dim = dim
        except Exception:
            self._embedding_provider = None
            self._embed_source = "local"

    @property
    def embed_source(self) -> str:
        return self._embed_source

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        prov = self._embedding_provider
        if prov is not None:
            try:
                vecs = await prov.get_embeddings(texts)
                return self._l2_normalize(vecs)
            except Exception:
                # provider 失败回退本地,不阻断主流程
                pass
        return _local_embed(texts)

    @staticmethod
    def _l2_normalize(vecs: list[list[float]]) -> list[list[float]]:
        if np is None:
            return vecs
        out: list[list[float]] = []
        for v in vecs:
            arr = np.asarray(v, dtype=np.float32).reshape(-1)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            out.append(arr.tolist())
        return out

    # ─── 路径 / 缓存 ──────────────────────────────────────────

    def _path(self, scope: str) -> str:
        safe = sanitize_key(scope)
        return os.path.join(self._root, f"{safe}.json")

    def _load_unlocked(self, scope: str) -> list[dict]:
        """持锁调用:从磁盘加载该 scope 全部条目(带 mtime 缓存)。"""
        path = self._path(scope)
        cached = self._cache.get(scope)
        mtime = 0.0
        if os.path.exists(path):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
        if cached is not None and cached[0] == mtime:
            return cached[1]
        if not os.path.exists(path):
            self._cache[scope] = (mtime, [])
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries") or []
        except (json.JSONDecodeError, OSError):
            entries = []
        self._cache[scope] = (mtime, entries)
        return entries

    def _persist_unlocked(self, scope: str, entries: list[dict]) -> None:
        """持锁调用:把条目写盘并更新缓存 mtime。"""
        path = self._path(scope)
        payload = {
            "dim": self._dim,
            "source": self._embed_source,
            "entries": entries,
        }
        write_json_atomic(path, payload)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = time.time()
        self._cache[scope] = (mtime, entries)

    # ─── 写 ───────────────────────────────────────────────────

    async def add(
        self,
        scope: str,
        content: str,
        turn: int = 0,
        importance: int = 1,
        metadata: dict | None = None,
        dedup_threshold: float = 0.9,
    ) -> str | None:
        """写入一条记忆,返回 id(与库内高度相似时跳过并返回已存在 id 或 None)。

        ``content`` 为空 / 过短(<4 字)直接丢弃。
        """
        content = (content or "").strip()
        if len(content) < 4:
            return None

        vector = (await self._embed([content]))[0]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._add_sync, scope, content, turn, importance, metadata, dedup_threshold, vector)

    def _add_sync(
        self,
        scope: str,
        content: str,
        turn: int,
        importance: int,
        metadata: dict | None,
        dedup_threshold: float,
        vector: list[float],
    ) -> str | None:
        with self._lock:
            entries = self._load_unlocked(scope)
            # 去重:与库内已有条目相似度过高则跳过(仅同维度可比)
            if entries and np is not None:
                for e in entries:
                    ev = e.get("vector")
                    if not ev or len(ev) != len(vector):
                        continue
                    sim = self._cosine(np.asarray(ev, dtype=np.float32), np.asarray(vector, dtype=np.float32))
                    if sim >= dedup_threshold:
                        # 更新其 turn / importance 以反映"最近又被提及"
                        e["turn"] = max(int(e.get("turn", 0)), turn)
                        e["importance"] = max(int(e.get("importance", 1)), importance)
                        if metadata:
                            e["metadata"] = dict(e.get("metadata") or {}, **metadata)
                        self._persist_unlocked(scope, entries)
                        return e.get("id")
            mid = "m_" + secrets.token_hex(6)
            entry = {
                "id": mid,
                "content": content,
                "turn": turn,
                "importance": int(importance),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "metadata": dict(metadata or {}),
                "vector": vector,
            }
            entries.append(entry)
            self._persist_unlocked(scope, entries)
            return mid

    @staticmethod
    def _cosine(a: Any, b: Any) -> float:
        if np is None:
            return 0.0
        try:
            d = float(np.dot(a, b))
            na = float(np.linalg.norm(a))
            nb = float(np.linalg.norm(b))
            if na == 0 or nb == 0:
                return 0.0
            return d / (na * nb)
        except Exception:
            return 0.0

    # ─── 检索 ─────────────────────────────────────────────────

    async def search(
        self,
        scope: str,
        query: str,
        top_k: int = 5,
        min_score: float = 0.1,
    ) -> list[dict]:
        """按 query 语义召回该 scope 中最相关的记忆。

        返回按 score 降序的条目列表(已注入 score / 去掉 vector),空库或
        所有相似度低于 min_score 时返回 []。
        """
        query = (query or "").strip()
        if not query:
            return await self.recent(scope, top_k)

        vector = (await self._embed([query]))[0]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._search_sync, scope, vector, top_k, min_score
        )

    def _search_sync(
        self, scope: str, vector: list[float], top_k: int, min_score: float
    ) -> list[dict]:
        with self._lock:
            entries = self._load_unlocked(scope)
            if not entries:
                return []
            scored: list[tuple[float, dict]] = []
            for e in entries:
                ev = e.get("vector")
                if not ev or (np is not None and len(ev) != len(vector)):
                    continue
                sim = self._cosine(np.asarray(ev, dtype=np.float32), np.asarray(vector, dtype=np.float32))
                if sim >= min_score:
                    scored.append((sim, e))
            scored.sort(key=lambda x: x[0], reverse=True)
            out = []
            for sim, e in scored[:top_k]:
                d = dict(e)
                d.pop("vector", None)
                d["score"] = round(float(sim), 4)
                out.append(d)
            return out

    async def recent(self, scope: str, limit: int = 5) -> list[dict]:
        """返回最近 N 条记忆(按写入顺序倒序,供无查询时兜底)。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._recent_sync, scope, limit)

    def _recent_sync(self, scope: str, limit: int) -> list[dict]:
        with self._lock:
            entries = self._load_unlocked(scope)
            out = []
            for e in reversed(entries):
                d = dict(e)
                d.pop("vector", None)
                out.append(d)
                if len(out) >= limit:
                    break
            return out

    # ─── 管理 ─────────────────────────────────────────────────

    async def count(self, scope: str) -> int:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._count_sync, scope)

    def _count_sync(self, scope: str) -> int:
        with self._lock:
            return len(self._load_unlocked(scope))

    async def delete_scope(self, scope: str) -> int:
        """删除整个 scope 的记忆库(会话删除 / 重建时调用),返回删除条数。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._delete_scope_sync, scope)

    def _delete_scope_sync(self, scope: str) -> int:
        with self._lock:
            entries = self._load_unlocked(scope)
            n = len(entries)
            path = self._path(scope)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            self._cache.pop(scope, None)
            return n

    async def set_max_entries(self, scope: str, max_entries: int) -> int:
        """裁剪 scope 记忆至最多 max_entries 条(LRU:丢最旧),返回删除条数。

        生命周期通常随会话删除,裁剪只是长期会话的安全阀。
        """
        if max_entries <= 0:
            return 0
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._trim_sync, scope, max_entries)

    async def replace_entries(self, scope: str, entries: list) -> None:
        """用给定条目列表整体覆盖该 scope 的记忆库(后台管理用)。

        条目须保留原 ``vector`` / 字段结构(通常取自 recent/search 返回)。
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._replace_entries_sync, scope, list(entries))

    def _replace_entries_sync(self, scope: str, entries: list) -> None:
        with self._lock:
            self._persist_unlocked(scope, entries)

    async def delete_entries_by_id(self, scope: str, ids: list) -> int:
        """按 id 删除 scope 内的若干条记忆,返回删除条数。"""
        idset = {str(x) for x in ids if x}
        if not idset:
            return 0
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._delete_by_id_sync, scope, idset)

    def _delete_by_id_sync(self, scope: str, idset: set) -> int:
        with self._lock:
            entries = self._load_unlocked(scope)
            before = len(entries)
            entries = [e for e in entries if str(e.get("id")) not in idset]
            removed = before - len(entries)
            if removed:
                self._persist_unlocked(scope, entries)
            return removed

    async def delete_entries_from_turn(self, scope: str, target_turn: int) -> int:
        """删除 turn 大于等于 target_turn 的记忆条目(供 /undo 回滚用)。

        /undo 回滚到 target_turn 意味着「回到第 target_turn 轮开始前」,
        因此**第 target_turn 轮及之后**产生的记忆(含 target_turn 轮自身的
        自动记录与 life_sim_memorize)都应清除。返回删除条数。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._delete_from_turn_sync, scope, int(target_turn))

    def _delete_from_turn_sync(self, scope: str, target_turn: int) -> int:
        with self._lock:
            entries = self._load_unlocked(scope)
            before = len(entries)
            entries = [
                e
                for e in entries
                if not (
                    isinstance(e.get("turn"), int)
                    and not isinstance(e.get("turn"), bool)
                    and e["turn"] >= target_turn
                )
            ]
            removed = before - len(entries)
            if removed:
                self._persist_unlocked(scope, entries)
            return removed

    def _trim_sync(self, scope: str, max_entries: int) -> int:
        with self._lock:
            entries = self._load_unlocked(scope)
            excess = len(entries) - max_entries
            if excess <= 0:
                return 0
            del entries[:excess]
            self._persist_unlocked(scope, entries)
            return excess
