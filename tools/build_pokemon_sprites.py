"""下载宝可梦缩略图(96x96 官方像素图)到 pokesim/static/sprites/。

数据源:PokeAPI/sprites 仓库的 `sprites/pokemon/<id>.png`(前身 96x96 缩略图)。
物种 key 与 PokeAPI pokemon identifier 通过归一化对齐(如 charizardmegax ↔
charizard-mega-x);未直接命中的用 baseSpecies / 全国图鉴号回退到本体图。

产出:`pokesim/static/sprites/<species_key>.png` + `sprites/index.json`(可选映射)。
运行一次即可,运行时不联网。
"""

from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, ".pokemon_cache")
SPECIES = os.path.join(ROOT, "pokesim", "static", "species.json")
OUT_DIR = os.path.join(ROOT, "pokesim", "static", "sprites")
BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/"
UA = "Mozilla/5.0 (compatible; astrbot-plugin-life-sim sprite builder)"

_SEP = set(" -_.·'’:\u3000()（）[]【】")


def norm(t) -> str:
    return "".join(c for c in str(t).lower() if c not in _SEP)


def load_pokemon_csv() -> list[dict]:
    path = os.path.join(CACHE_DIR, "pokemon.csv")
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_id_map() -> dict[str, str]:
    """species key → PokeAPI pokemon id(含 base/num 回退)。"""
    rows = load_pokemon_csv()
    direct: dict[str, str] = {}
    default_by_species_id: dict[str, str] = {}
    for r in rows:
        direct.setdefault(norm(r["identifier"]), r["id"])
        if r["id"] == r["species_id"]:
            default_by_species_id[r["species_id"]] = r["id"]

    species = json.load(open(SPECIES, encoding="utf-8"))
    out: dict[str, str] = {}
    for key, entry in species.items():
        pid = direct.get(norm(key))
        if not pid:
            base = entry.get("baseSpecies")
            if base:
                pid = direct.get(norm(base))
        if not pid:
            num = entry.get("num")
            if num and str(num) in default_by_species_id:
                pid = default_by_species_id[str(num)]
        if pid:
            out[key] = pid
    return out


def fetch(pid: str, dest: str) -> tuple[str, int]:
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return "skip", 0
    for attempt in range(3):
        try:
            req = urllib.request.Request(BASE + f"{pid}.png", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = resp.read()
            if not data:
                return "empty", 0
            with open(dest, "wb") as fh:
                fh.write(data)
            return "ok", len(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return "404", 0
        except Exception:  # noqa: BLE001
            pass
    return "fail", 0


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    idmap = build_id_map()
    species = json.load(open(SPECIES, encoding="utf-8"))
    print(f"物种 {len(species)},可映射 {len(idmap)}")

    stats = {"ok": 0, "skip": 0, "404": 0, "fail": 0, "empty": 0, "bytes": 0}
    fail_keys: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for key, pid in idmap.items():
            dest = os.path.join(OUT_DIR, f"{key}.png")
            futures[pool.submit(fetch, pid, dest)] = key
        for i, fut in enumerate(concurrent.futures.as_completed(futures)):
            key = futures[fut]
            try:
                kind, size = fut.result()
            except Exception:  # noqa: BLE001
                kind, size = "fail", 0
            stats[kind] = stats.get(kind, 0) + 1
            stats["bytes"] += size
            if kind in ("fail", "404", "empty"):
                fail_keys.append(key)
            if i % 200 == 0:
                print(f"  ... {i}/{len(idmap)}")

    with open(os.path.join(OUT_DIR, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(idmap, fh, ensure_ascii=False, separators=(",", ":"))
    have = len([f for f in os.listdir(OUT_DIR) if f.endswith(".png")])
    print(
        f"完成: 图片 {have} 张,新增 {stats['ok']},跳过 {stats['skip']},"
        f"404 {stats['404']},失败 {stats['fail']},合计 {stats['bytes'] // 1024} KB"
    )
    if fail_keys:
        print(f"  未取得({len(fail_keys)}): {fail_keys[:12]}")


if __name__ == "__main__":
    main()
