#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LevelGridSolver 离线验证（P2 设计 §5）——GT 只进这里。

对 JC1/ZC1 跑投票网格，测对 GT 层表命中率（tol 150/250/300 三档），
输出分层报告（命中/未命中/网格独有，各层 provenance），落
out/<tower>/level_grid_validation.json。

门禁：可绘制区（≥ 最低 datum - 600mm）命中率 < 85% → exit 1。

纪律：验证目标表 = 被 LevelGridSolver 替换的对象本身——
ZC1 用 overlay 的 gt_*_override 表；JC1 overlay 无 override（回退
gt_profile 常数），故仅本脚本允许 import gt_profile。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.solve.level_grid import grid_from_sheets_dir  # noqa: E402

TOWERS = {
    "jc1": {
        "sheets": "out/35A1-JC1-full-deliver/sheets",
        "overlay": "examples/external/guowang_35A1/layer_overlay.json",
        "out": "out/35A1-JC1-full-deliver/level_grid_validation.json",
        "gt_source": "gt_profile",
    },
    "zc1": {
        "sheets": "out/35A2-ZC1-full-deliver/sheets",
        "overlay": "examples/external/guowang_35A2_zc1/layer_overlay.json",
        "out": "out/35A2-ZC1-full-deliver/level_grid_validation.json",
        "gt_source": "overlay",
    },
}
GATE_MIN_HIT = 0.85
TOLS = (150.0, 250.0, 300.0)


def _hit(levels, target, tol):
    return any(abs(target - l) <= tol for l in levels)


def _hit_stats(levels, targets, tol):
    hit = [t for t in targets if _hit(levels, t, tol)]
    return len(hit), len(targets), (len(hit) / len(targets) if targets else 1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tower", choices=sorted(TOWERS), required=True)
    args = ap.parse_args()
    cfg = TOWERS[args.tower]

    overlay = json.loads(Path(cfg["overlay"]).read_text(encoding="utf-8"))
    levels, records, warnings = grid_from_sheets_dir(Path(cfg["sheets"]), overlay)

    # 验证目标（被替换的 GT 层表）
    if cfg["gt_source"] == "overlay":
        targets_term = [float(z) for z in overlay.get("gt_terminal_levels_override") or []]
        targets_plat = [float(z) for z in overlay.get("gt_platform_levels_override") or []]
        src_desc = "overlay gt_*_override"
    else:
        from traceability.debug.gt_profile import (
            gt_diagonal_terminal_levels, gt_platform_levels)
        targets_term = [float(z) for z in gt_diagonal_terminal_levels()]
        targets_plat = [float(z) for z in gt_platform_levels()]
        src_desc = "gt_profile 常数表"
    targets_all = sorted(set(targets_term) | set(targets_plat))

    # 可绘制区：≥ 最低正 datum - 600（低于最低图册覆盖的结构性不可达；
    # datum=0.0 是未标定册（如 JC1-40 基础详图）的占位，不参与）
    datums = [float(r.get("z_offset")) for regs in (overlay.get("view_regions") or {}).values()
              if isinstance(regs, list) for r in regs
              if isinstance(r, dict) and r.get("z_offset")
              and float(r["z_offset"]) > 0]
    z_min_draw = min(datums) - 600.0 if datums else 0.0
    drawable_term = [t for t in targets_term if t >= z_min_draw]

    report = {
        "tower": args.tower,
        "gt_source": src_desc,
        "n_grid_levels": len(levels),
        "grid_levels": levels,
        "records": records,
        "warnings": warnings,
        "z_min_drawable": z_min_draw,
        "hit_rates": {},
        "missed": {},
        "spurious": [],
    }
    print(f"=== LevelGridSolver 验证 · {args.tower} ===")
    print(f"网格: {len(levels)} 层 | 目标: terminal {len(targets_term)} / "
          f"platform {len(targets_plat)}（{src_desc}）")
    print(f"可绘制区下界: ≥ {z_min_draw:.0f}（terminal 可绘制 {len(drawable_term)} 层）")
    for tol in TOLS:
        h, n, r = _hit_stats(levels, targets_term, tol)
        hd, nd, rd = _hit_stats(levels, drawable_term, tol)
        hp, np_, rp = _hit_stats(levels, targets_plat, tol)
        report["hit_rates"][f"tol_{int(tol)}"] = {
            "terminal": {"hit": h, "n": n, "rate": round(r, 4)},
            "terminal_drawable": {"hit": hd, "n": nd, "rate": round(rd, 4)},
            "platform": {"hit": hp, "n": np_, "rate": round(rp, 4)},
        }
        print(f"tol={tol:.0f}: terminal {h}/{n}={r:.1%} | "
              f"可绘制 {hd}/{nd}={rd:.1%} | platform {hp}/{np_}={rp:.1%}")
    missed = [t for t in drawable_term if not _hit(levels, t, 300.0)]
    report["missed"]["terminal_drawable_tol300"] = missed
    spurious = [l for l in levels if not _hit(targets_all, l, 300.0)]
    report["spurious"] = spurious
    print(f"未命中(可绘制, tol300): {missed}")
    print(f"网格独有层(±300 无 GT 对应): n={len(spurious)} → {[round(z) for z in spurious]}")

    rate = report["hit_rates"]["tol_300"]["terminal_drawable"]["rate"]
    gate_pass = rate >= GATE_MIN_HIT
    report["gate"] = {"min_hit": GATE_MIN_HIT, "rate": rate, "pass": gate_pass}

    out = Path(cfg["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告: {out}")
    print(("✓" if gate_pass else "✗") + f" 门禁（可绘制区命中 ≥ {GATE_MIN_HIT:.0%}）: {rate:.1%}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
