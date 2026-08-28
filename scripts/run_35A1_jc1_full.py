#!/usr/bin/env python3
"""35A1-JC1 全册跑批：JC1 文件夹内所有图纸参与解析与工程追溯。

空间 3D 合并仍只用正立面 02 + 基础平面（不能把 40+ 张详图都当正立面叠在一起）。
全册详图/模块页会解析杆件并写入各 sheet JSON，供 BOM / Harness / 追溯。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OVERLAY_PATH = REPO / "examples/external/guowang_35A1/layer_overlay.json"
DXF_BATCH = REPO / "out/xianyu-acceptance/batch-jc1/dxf"
OUT = REPO / "out/35A1-JC1-full-deliver"
DEMO_DIR = REPO / "web/demo/35A1-JC1"
GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"


def full_overlay() -> dict:
    ov = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    cf = dict(ov.get("cross_file_views") or {})
    cf["parse_all_project_sheets"] = True
    ov["cross_file_views"] = cf
    return ov


def bar_stats(model) -> dict:
    from scripts.diagnose_35A1_jc1 import bar_graph_stats

    return bar_graph_stats(model)


def sheet_bar_summary(cross_file_dir: Path) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    for p in sorted(cross_file_dir.glob("35A1-JC1-*.json")):
        from traceability.io import load_model

        m = load_model(str(p))
        bars = sum(1 for c in m.components.values() if c.kind == "tower_bar")
        if bars:
            rows.append((p.stem, bars))
    return sorted(rows, key=lambda x: -x[1])


def main() -> int:
    import subprocess

    from traceability.intake.tower_spec import cross_file_merge_stems
    from traceability.io import load_model
    from traceability.project.delivery import deliver_project
    from traceability.solve.tower_solver import tower_geometry_gate

    overlay = full_overlay()
    spatial_stems = sorted(cross_file_merge_stems(overlay))
    print("JC1 全册 DXF 目录:", DXF_BATCH)
    print("全册解析: parse_all_project_sheets=True")
    print("空间 3D 合并 stems:", ", ".join(spatial_stems))

    if not DXF_BATCH.exists():
        print(f"DXF 目录不存在: {DXF_BATCH}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    pd = deliver_project(
        DXF_BATCH,
        layer_map_path=overlay,
        bom_path=str(REPO / "examples/external/guowang_35A1/guowang_merged_bom.csv"),
        project_id="35A1-JC1-full",
        out_dir=OUT,
    )

    model = load_model(str(OUT / "model.json"))
    stats = bar_stats(model)
    gate = tower_geometry_gate(model, overlay)

    sheet_rows = sheet_bar_summary(OUT / "cross_file")
    total_sheet_bars = sum(b for _, b in sheet_rows)

    print()
    print("=== 35A1-JC1 全册交付 ===")
    print(f"deliver ok: {pd.get('ok')}")
    print(f"空间 merge_stems: {pd.get('merge_report', {}).get('merge_stems')}")
    print(f"全册有杆件的分册: {len(sheet_rows)} 张，合计 {total_sheet_bars} 根（各分册独立解析）")
    print(f"3D 合并模型杆件: {stats['bars']}")
    print(f"3D 节点: {stats['nodes']}")
    print(f"塔高 bbox Z: {stats['bbox_mm']['z']}")
    print(f"门禁: {'通过' if gate['ok'] else '失败'}")
    if gate.get("reasons"):
        for r in gate["reasons"]:
            print(f"  - {r}")

    if GT_PATH.exists():
        ev = subprocess.run(
            [sys.executable, str(REPO / "scripts/evaluate_ground_truth.py"),
             str(GT_PATH), str(OUT / "model.json"), "--view", "front"],
            capture_output=True, text=True,
        )
        print()
        print("=== GT 评测（3D 合并模型）===")
        for ln in ev.stdout.strip().splitlines():
            print(ln)

    glb = OUT / "tower.glb"
    if glb.exists():
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(glb, DEMO_DIR / "tower_from_dxf.glb")
        print(f"\nGLB: {glb} ({glb.stat().st_size // 1024} KB)")
        print(f"预览: http://127.0.0.1:8000/demo/35A1-JC1/compare.html")

    report = {
        "spatial_merge_stems": spatial_stems,
        "sheet_bar_summary": sheet_rows,
        "total_sheet_bars": total_sheet_bars,
        "deliver": pd,
        "stats_3d": stats,
        "gate": gate,
    }
    (OUT / "full_run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"报告: {OUT / 'full_run_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
