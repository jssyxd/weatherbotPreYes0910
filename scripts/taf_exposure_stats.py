#!/usr/bin/env python3
"""无 TAF 暴露面统计 —— 审计字段（2026-09-12 追加）的**查询模板**。

`re_fire` / `breach_risk_control` 的审计行现在带 4 个纯追加字段：

    taf_present : bool  —— 该站在本次评估时是否有可用 TAF（True ⇒ 走 TAF 极值分支）
    taf_source  : str   —— "taf" / "market_rank1"
    fire_path   : str   —— "entry" / "hedge" / "refire"
    fire_key    : str   —— 该 fire 的会话 key

本脚本只用标准库读 JSONL（`data/yes2re_events.jsonl` 或任何同构日志），按
(fire_path × taf_present) 计数，并给出"对冲路径中 taf_present=false"的**具体次数**。

用法::

    python3 scripts/taf_exposure_stats.py data/yes2re_events.jsonl
    python3 scripts/taf_exposure_stats.py data/*.jsonl --path hedge --type fire_attempt

退出码: 0 = 读到至少一行；2 = 没有可读文件 / 一个事件都没有；1 = 用法错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

#: 带 fire 语义的事件类型（追加字段落在这几类行上）
FIRE_EVENTS = ("fire", "fire_attempt", "breach_risk_control")
PATHS = ("entry", "hedge", "refire")


def load_rows(paths: list[str]) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    skipped: list[str] = []
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            skipped.append(f"{raw}: not a file")
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001 — a torn last line must not kill the report
                skipped.append(f"{raw}: unparseable line")
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows, skipped


def summarise(rows: list[dict], *, only_path: str | None = None,
              only_type: str | None = None, only_key: str | None = None) -> dict:
    by_path: Counter = Counter()
    by_path_taf: Counter = Counter()
    by_source: Counter = Counter()
    missing_fields = 0
    seen = 0
    for row in rows:
        etype = str(row.get("type") or "")
        if etype not in FIRE_EVENTS:
            continue
        if only_type and etype != only_type:
            continue
        path = row.get("fire_path")
        if path is None or "taf_present" not in row:
            missing_fields += 1          # 追加字段上线前的旧行（或非 fire 路径行）
            continue
        if only_path and path != only_path:
            continue
        if only_key and row.get("fire_key") != only_key:
            continue
        seen += 1
        by_path[str(path)] += 1
        by_path_taf[(str(path), bool(row.get("taf_present")))] += 1
        by_source[str(row.get("taf_source"))] += 1
    return {"rows_seen": seen, "by_path": by_path, "by_path_taf": by_path_taf,
            "by_source": by_source, "rows_without_audit_fields": missing_fields}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="无 TAF 暴露面统计（审计字段查询模板）")
    ap.add_argument("jsonl", nargs="+", help="一个或多个事件 JSONL（如 data/yes2re_events.jsonl）")
    ap.add_argument("--path", choices=PATHS, help="只看某条路径（entry/hedge/refire）")
    ap.add_argument("--type", dest="etype", choices=FIRE_EVENTS, help="只看某类事件")
    ap.add_argument("--key", help="只看某个会话 key")
    args = ap.parse_args(argv)

    rows, skipped = load_rows(args.jsonl)
    if not rows:
        print("no readable events found", file=sys.stderr)
        return 2
    out = summarise(rows, only_path=args.path, only_type=args.etype, only_key=args.key)
    print(f"files={len(args.jsonl)}  fire-事件行={out['rows_seen']}"
          f"  (缺追加字段的旧行={out['rows_without_audit_fields']})")
    for path in PATHS:
        fired = out["by_path_taf"].get((path, True), 0)
        no_taf = out["by_path_taf"].get((path, False), 0)
        if fired or no_taf:
            print(f"  fire_path={path:<6} 总={fired + no_taf:>4}  taf_present=True={fired:>4}"
                  f"  taf_present=False={no_taf:>4}")
    hedge_no_taf = out["by_path_taf"].get(("hedge", False), 0)
    print(f"对冲路径中 taf_present=False 的次数 = {hedge_no_taf}")
    if out["by_source"]:
        print("taf_source 分布:", dict(out["by_source"]))
    for note in skipped[:5]:
        print(f"  [warn] {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
