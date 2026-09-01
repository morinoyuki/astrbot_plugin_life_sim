#!/usr/bin/env python3
"""一次性迁移脚本:把旧格式(内联整份 lore / RPG 快照)的会话文件压缩为新格式。

插件升级后,下一次 /do 或 /undo 会自然迁移,无需手动操作。
若想立即压缩现存会话文件(尤其是长期运行的会话,如 sim_sessions 里几 MB 的
文件),可运行本脚本对指定 data_dir 做一次遍历迁移:

    .venv/bin/python tools/migrate_snapshots.py <data_dir> [--dry-run] [--backup]

- 对 sim_sessions/*.json、sim_branches/*/*.json 逐一加载 → 调用
  _compact_lore_versions / _compact_rpg_versions → 写回(原子写)。
- 兼容性:新格式文件幂等不变;旧格式文件就地收敛。
- --dry-run:只统计不改盘。--backup:改写前把原文件复制为 `*.bak`。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "lsim_pkg", os.path.join(_ROOT, "__init__.py"),
    submodule_search_locations=[_ROOT],
)
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["lsim_pkg"] = _pkg
_spec.loader.exec_module(_pkg)

from lsim_pkg.main import _compact_lore_versions, _compact_rpg_versions


def _migrate_one(path: str, dry_run: bool, backup: bool) -> tuple[int, int]:
    """返回 (旧字节, 新字节);文件不存在/损坏返回 None。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            session = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    before = len(json.dumps(session, ensure_ascii=False))
    _compact_lore_versions(session)
    _compact_rpg_versions(session)
    after = len(json.dumps(session, ensure_ascii=False))
    if dry_run or before == after:
        return before, after
    if backup:
        shutil.copy2(path, path + ".bak")
    from lsim_pkg.storage_base import write_json_atomic

    write_json_atomic(path, session)
    return before, after


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data_dir", help="插件 data_dir(含 sim_sessions / sim_branches)")
    ap.add_argument("--dry-run", action="store_true", help="只统计,不改盘")
    ap.add_argument("--backup", action="store_true", help="改写前备份为 *.bak")
    args = ap.parse_args()

    targets: list[str] = []
    sim_dir = os.path.join(args.data_dir, "sim_sessions")
    if os.path.isdir(sim_dir):
        targets += [
            os.path.join(sim_dir, f)
            for f in os.listdir(sim_dir)
            if f.endswith(".json")
        ]
    br_root = os.path.join(args.data_dir, "sim_branches")
    if os.path.isdir(br_root):
        for scope in os.listdir(br_root):
            sd = os.path.join(br_root, scope)
            if os.path.isdir(sd):
                targets += [
                    os.path.join(sd, f)
                    for f in os.listdir(sd)
                    if f.endswith(".json")
                ]

    total_before = total_after = 0
    n_changed = 0
    for p in sorted(targets):
        r = _migrate_one(p, args.dry_run, args.backup)
        if r is None:
            print(f"  ⏭  {p} (跳过/不可读)")
            continue
        b, a = r
        total_before += b
        total_after += a
        if a < b:
            n_changed += 1
            pct = (1 - a / b) * 100
            print(f"  ✂  {p}: {b:,} -> {a:,}B (-{pct:.1f}%)")
        else:
            print(f"  ✓  {p}: 已是最新格式 ({a:,}B)")
    if total_before:
        print(
            f"\n{'[dry-run] ' if args.dry_run else ''}合计:{n_changed} 个文件变更,"
            f"{total_before:,} -> {total_after:,}B "
            f"(-{(1 - total_after / total_before) * 100:.1f}%)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
