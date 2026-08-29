#!/usr/bin/env python3
"""从 35A1-JC1 各段 DXF 图纸提取完整材料表，并映射到 GT 杆件。

发现：master BOM（guowang_merged_bom.csv，60 件号 101~160）只是「02 段（塔头）」
的材料表子集。全塔材料表分散在 02~10 段共 9 张结构图里，每段有独立件号段
（02: 101~160, 03: 201~223, 04: 301~341, 05: 401~427, ...）。

本脚本：
1. 用 parse_bom_dxf_anchored 从 02~10 段图纸提取完整材料表（252 件号，∑qty≈1026）
2. 用 build_bar_id_mapping 映射到 GT 的 PM_XXXX 杆件
3. 输出 full_bom.json + full_bar_id_mapping.json

用法：python3 scripts/extract_full_bom_35A1_jc1.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DXF_DIR = REPO / "out/xianyu-acceptance/batch-jc1/dxf"
GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"
OUT_DIR = REPO / "out/35A1-JC1-full-deliver"

SEGMENTS = ["02", "03", "04", "05", "06", "07", "08", "09", "10"]


def extract_full_bom() -> list[dict]:
    """从 02~10 段图纸提取完整材料表。"""
    from traceability.intake.tower_bom import parse_bom_dxf_anchored

    rows: list[dict] = []
    for seg in SEGMENTS:
        p = DXF_DIR / f"35A1-JC1-{seg}.dxf"
        if not p.exists():
            print(f"  跳过 {seg}（无图纸）")
            continue
        try:
            raw = parse_bom_dxf_anchored(str(p))
        except Exception as exc:
            print(f"  {seg} 解析失败: {exc}")
            continue
        seg_rows = [
            r for r in raw
            if str(r.get("bar_id", "")).isdigit() and 100 <= int(r["bar_id"]) <= 999
        ]
        for r in seg_rows:
            rows.append({
                "bar_id": str(r["bar_id"]),
                "section": (r.get("section") or "").strip(),
                "length_mm": str(int(r.get("length_mm", 0) or 0)),
                "qty": str(int(r.get("qty", 1) or 1)),
                "segment": seg,
            })
    return rows


def main() -> int:
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    bom = extract_full_bom()
    total_qty = sum(int(r["qty"]) for r in bom)
    print(f"完整材料表: {len(bom)} 件号, ∑qty={total_qty}")
    print(f"GT 结构杆: {len(gt['bars'])}")

    from traceability.project.bar_id_mapping import build_bar_id_mapping

    result = build_bar_id_mapping(gt, bom)
    mapped_gt = set()
    for _bid, m in result["mapping"].items():
        mapped_gt.update(m.get("gt_ids", []))
    print(f"映射: {result['assigned']}/{result['total']} 件号, 覆盖 {len(mapped_gt)}/{len(gt['bars'])} 根 GT 杆")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "full_bom.json").write_text(
        json.dumps(bom, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "full_bar_id_mapping.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n产物:")
    print(f"  {OUT_DIR / 'full_bom.json'}")
    print(f"  {OUT_DIR / 'full_bar_id_mapping.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
