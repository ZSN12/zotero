#!/usr/bin/env python3
"""工作区预检（validate_workspace）。

开源基座对标（2026-09-03）：跑批之前把关配置纪律——
「换塔只改配置」的前提是配置本身合法。五类检查：

  1. 结构：dxf/ 有图纸；bom/bom.csv 存在（或 overlay 声明 master_bom）。
  2. overlay：合法 JSON；view_regions 键与 dxf/ 册名对得上
     （声明的册必须有图，图里没声明的册只警告——可能是详图/材料表）。
  3. GT 注入纪律（铁律 1）：任何 gt_* / use_gt_* 键必须是 versioning.py
     已登记的 z-only 面——未知注入面直接 FAIL（fail-closed，对应
     SKILL.md 硬性要求 3：新增注入面必须先登记 versioning）。
  4. BOM：可解析，且至少一行判 member（Bug A 教训：件号形态不认识 →
     BOM 交叉核验整层静默消失）。
  5. GT（可选）：若 gt/ground_truth.json 存在，必须带非空 caveats。

退出码：0 可跑批；1 问题逐条列出。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

# versioning.py _gt_keys + override 清单的镜像（z-only 允许面）。
# 新增注入面 → 先改 traceability/project/versioning.py 再来这里同步，
# 否则视为未披露（SKILL.md 硬性要求 3）。
KNOWN_GT_SURFACES = {
    "use_gt_platform_levels", "use_gt_half_width", "use_gt_diaphragm_levels",
    "gt_platform_levels_override", "gt_terminal_levels_override",
    "gt_diaphragm_levels_override", "terminal_pair_span_whitelist",
    "terminal_pair_structure", "panel_level_source",
}


def _gt_surface_keys(overlay: dict) -> list:
    return [k for k in overlay
            if (k.startswith("gt_") or k.startswith("use_gt_"))
            and k not in ("gt_align",)]


def validate(ws: Path) -> tuple[list, list]:
    problems, warnings = [], []

    # ---- 1) 结构 ----
    dxf_dir = ws / "dxf"
    if not dxf_dir.is_dir():
        problems.append(f"无 dxf/ 目录：{dxf_dir}")
    else:
        dxfs = sorted(dxf_dir.glob("*.dxf")) + sorted(dxf_dir.glob("*.dwg"))
        if not dxfs:
            problems.append(f"dxf/ 目录为空：{dxf_dir}")
    bom = ws / "bom" / "bom.csv"
    if not bom.exists():
        problems.append(f"无 BOM：{bom}（多册塔还需要 overlay master_bom 路径）")

    overlay_path = ws / "overlay.json"
    overlay = None
    if not overlay_path.exists():
        if dxf_dir.is_dir():
            n = len(list(dxf_dir.glob("*.dxf"))) + len(list(dxf_dir.glob("*.dwg")))
            if n > 1:
                problems.append(
                    f"多册塔缺 overlay.json：{overlay_path}"
                    "（view_regions/cross_file_views 必填）")
            else:
                warnings.append("单册无 overlay（走 run_tower 路径，不启用跨册合并）")
    else:
        try:
            overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"overlay.json 不可解析：{exc}")
            overlay = None

    # ---- 2) overlay 册名一致性 ----
    if isinstance(overlay, dict):
        regions = overlay.get("view_regions")
        if isinstance(regions, dict):
            stems = {p.stem for p in dxf_dir.glob("*")} if dxf_dir.is_dir() else set()
            for sheet in regions:
                if sheet not in stems:
                    problems.append(
                        f"view_regions 声明册 {sheet!r} 在 dxf/ 找不到对应图纸")
            declared = set(regions)
            for p in sorted(stems):
                if p.endswith(".dxf") or p.endswith(".dwg"):
                    continue
                if p not in declared:
                    warnings.append(
                        f"图纸 {p} 未在 view_regions 声明（详图/材料表可忽略）")
        z_tables = {
            "gt_platform_levels_override": overlay.get("gt_platform_levels_override"),
            "gt_terminal_levels_override": overlay.get("gt_terminal_levels_override"),
            "gt_diaphragm_levels_override": overlay.get("gt_diaphragm_levels_override"),
        }
        for key, val in z_tables.items():
            if val and not (isinstance(val, list) and all(
                    isinstance(z, (int, float)) for z in val)):
                problems.append(f"overlay {key} 必须是数值 z 列表（只许 z，不许 x/y）")
        wl = overlay.get("terminal_pair_span_whitelist")
        if wl and not (isinstance(wl, list) and all(
                isinstance(p_, (list, tuple)) and len(p_) == 2 for p_ in wl)):
            problems.append("terminal_pair_span_whitelist 必须是 [[z_lo, z_hi], ...]")

    # ---- 3) GT 注入纪律（fail-closed）----
    if isinstance(overlay, dict):
        unknown = [k for k in _gt_surface_keys(overlay)
                   if k not in KNOWN_GT_SURFACES]
        if unknown:
            problems.append(
                f"overlay 含未登记的 GT 注入面 {unknown}——铁律 1 fail-closed。"
                "若是新的 z-only 面，先在 traceability/project/versioning.py "
                "登记（SKILL.md 硬性要求 3），否则视为未披露注入。")
        # 粗粒度 x/y 注入形态直接拦截
        for k in overlay:
            if k.startswith("gt_") and ("x" in k.lower() or "y" in k.lower()) \
                    and k not in KNOWN_GT_SURFACES:
                problems.append(f"疑似 x/y 注入面：{k}（铁律 1：严禁注入 GT x/y）")

    # ---- 4) BOM 可解析 + member 行 ----
    if bom.exists():
        try:
            import csv
            from traceability.intake.tower_bom import classify_bom_row
            with bom.open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                problems.append("BOM 空表（只有表头）")
            n_member = sum(
                1 for r in rows
                if classify_bom_row(r.get("bar_id", ""), r.get("section", "")) == "member")
            if rows and n_member == 0:
                problems.append(
                    "BOM 无一行判 member——BOM 交叉核验将整层静默消失"
                    "（Bug A 教训：检查件号形态/列对齐）")
        except (ValueError, OSError) as exc:
            problems.append(f"BOM 不可读：{exc}")

    # ---- 5) GT caveats ----
    gt_path = ws / "gt" / "ground_truth.json"
    if gt_path.exists():
        try:
            gt = json.loads(gt_path.read_text(encoding="utf-8"))
            if not gt.get("caveats"):
                problems.append(
                    "gt/ground_truth.json 无非空 caveats（GT 来源等级必须披露——"
                    ".mod 直出可并列呈报；GLB 反提取仅限内部回归）")
        except ValueError as exc:
            problems.append(f"gt/ground_truth.json 不可解析：{exc}")

    return problems, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("workspace", type=Path, help="init_domain 生成的工作区目录")
    args = ap.parse_args()

    ws = args.workspace.resolve()
    if not ws.is_dir():
        print(f"工作区不存在：{ws}", file=sys.stderr)
        return 1

    problems, warnings = validate(ws)
    for w in warnings:
        print(f"  ⚠ {w}")
    if problems:
        print(f"validate_workspace: {len(problems)} 项问题（不可跑批）")
        for p in problems:
            print("  ✗", p)
        return 1
    print("validate_workspace: 通过（配置纪律 OK，可跑 run_layer 1..6）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
