#!/usr/bin/env python3
"""从 35A2-ZC1 各段 DXF 图纸提取完整材料表，并映射到 GT 杆件。

背景（ZC1 阶段 2+4 前置，2026-09-05）：
  * ZC1 overlay 的 master_bom=guowang_merged_bom.csv 只存在于 35A1
    目录（JC1 塔），ZC1 侧解析为 None → bom_tree 全部 length_mm=0。
  * ZC1 六册（05/07/08/09/10/12）图纸内各自有材料表（件号/规格/
    长度/数量），parse_bom_dxf_anchored 可直接读取。
  * 塔头横担（z 33000/33500/34000/35800）在 ZC1 六册无立面画线
    （宽杆画线最高 z~24000），58 根头部 FN 的唯一图纸证据是
    BOM 长度（阶段 0 实测 50/58 在 BOM ±50 内有对应长度）。

本脚本：
1. 用 parse_bom_dxf_anchored 从六册图纸提取完整材料表（~202 件号）
2. 用 build_bar_id_mapping 映射到 GT 的 PM_XXXX 杆件（研究侧验证，
   不进管线——管线的 parametric 生成只允许消费 BOM 行本身）
3. 输出 full_bom.json + full_bar_id_mapping.json 到 a2plan 目录

用法：python3 scripts/extract_full_bom_35A2_zc1.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DXF_DIR = REPO / "out/xianyu-acceptance/batch-zc1/dxf"
GT_PATH = REPO / "examples/gt/35A2-ZC1_ground_truth.json"
OUT_DIR = REPO / "out/35A2-ZC1-a2plan"

SHEETS = ["05", "07", "08", "09", "10", "12"]


def extract_full_bom() -> list[dict]:
    """从六册图纸提取完整材料表（去重合并，保留首见 sheet）。"""
    from traceability.intake.tower_bom import parse_bom_dxf_anchored

    seen: dict[str, dict] = {}
    for seg in SHEETS:
        p = DXF_DIR / f"35A2-ZC1-{seg}.dxf"
        if not p.exists():
            print(f"  跳过 {seg}（无图纸）")
            continue
        try:
            raw = parse_bom_dxf_anchored(str(p))
        except Exception as exc:
            print(f"  {seg} 解析失败: {exc}")
            continue
        n_seg = 0
        for r in raw:
            bid = str(r.get("bar_id", "")).strip()
            if not bid.isdigit() or not (100 <= int(bid) <= 9999):
                continue
            if bid in seen:
                # 跨册重复件号：保留长度更完整的行
                old_len = int(seen[bid].get("length_mm") or 0)
                new_len = int(r.get("length_mm") or 0)
                if new_len > old_len:
                    seen[bid].update({
                        "section": (r.get("section") or "").strip(),
                        "length_mm": str(new_len),
                        "qty": str(int(r.get("qty", 1) or 1)),
                    })
                continue
            seen[bid] = {
                "bar_id": bid,
                "section": (r.get("section") or "").strip(),
                "length_mm": str(int(r.get("length_mm", 0) or 0)),
                "qty": str(int(r.get("qty", 1) or 1)),
                "segment": seg,
            }
            n_seg += 1
        print(f"  {seg}: {len(raw)} 行（新增 {n_seg}）")
    return list(seen.values())


def main() -> int:
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    bom = extract_full_bom()
    total_qty = sum(int(r["qty"]) for r in bom)
    n_len = sum(1 for r in bom if int(r["length_mm"]) > 0)
    print(f"完整材料表: {len(bom)} 件号, ∑qty={total_qty}, 带长度 {n_len}")
    print(f"GT 结构杆: {len(gt['bars'])}")

    from traceability.project.bar_id_mapping import build_bar_id_mapping

    result = build_bar_id_mapping(gt, bom)
    mapped_gt = set()
    for _bid, m in result["mapping"].items():
        mapped_gt.update(m.get("gt_ids", []))
    print(f"映射: {result['assigned']}/{result['total']} 件号, "
          f"覆盖 {len(mapped_gt)}/{len(gt['bars'])} 根 GT 杆")

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
