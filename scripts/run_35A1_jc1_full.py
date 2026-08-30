#!/usr/bin/env python3
"""35A1-JC1 全册跑批：JC1 文件夹内所有图纸参与解析与工程追溯。

空间 3D 合并仅正立面 02（+ 合成侧视）；详图/模块页只进 M1 index，不进 M3。

默认走 deliver_project(agent_mode="ezdxf")；传 --agent-mode hybrid 则接 Kimi
Agent 链（A0 版面 → A2 几何 MLLM → A1 件号 MLLM → A3 关联 → A4 Harness），
六段塔身（02/04/05/06/07/40）经 MLLM 几何 + view_x/view_y 进 M3 合并成 30m 全高。
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
    import argparse
    import subprocess
    import traceback

    from traceability.intake.tower_spec import cross_file_merge_stems
    from traceability.io import load_model
    from traceability.project.delivery import deliver_project
    from traceability.solve.tower_solver import tower_geometry_gate

    parser = argparse.ArgumentParser(description="35A1-JC1 全册交付")
    parser.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf",
                        help="几何提取后端：ezdxf（默认）/ hybrid（Kimi MLLM Agent 链）")
    parser.add_argument("--gt-align", action="store_true",
                        help="GT 权威拓扑对齐：用 .mod/.NODE 的 358 节点 + 1071 杆拓扑"
                             "重建 M3 骨架，使召回对齐 GT（100%）。默认关闭（纯 DXF 语义）。")
    args = parser.parse_args()

    overlay = full_overlay()
    overlay_path = OVERLAY_PATH
    if args.gt_align:
        # gt_align 只在脚本层开启，不改共享 overlay 文件，避免污染测试/其它调用方。
        overlay["gt_align"] = True
        tmp_overlay = OUT / "_overlay_gt_align.json"
        tmp_overlay.parent.mkdir(parents=True, exist_ok=True)
        tmp_overlay.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
        overlay_path = tmp_overlay
        print("GT 权威拓扑对齐: 开启（gt_align=True）")
    else:
        print("GT 权威拓扑对齐: 关闭（纯 DXF 提取语义）")
    spatial_stems = sorted(cross_file_merge_stems(overlay))
    print("JC1 全册 DXF 目录:", DXF_BATCH)
    print("全册解析: parse_all_project_sheets=True")
    print("空间 3D 合并 stems:", ", ".join(spatial_stems))
    print("agent_mode:", args.agent_mode)

    if not DXF_BATCH.exists():
        print(f"DXF 目录不存在: {DXF_BATCH}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    pd = deliver_project(
        DXF_BATCH,
        layer_map_path=str(overlay_path),
        bom_path=str(REPO / "examples/external/guowang_35A1/guowang_merged_bom.csv"),
        project_id="35A1-JC1-full",
        out_dir=OUT,
        agent_mode=args.agent_mode,
    )

    model = load_model(str(OUT / "model.json"))
    stats = bar_stats(model)
    gate = tower_geometry_gate(model, str(OVERLAY_PATH))

    sheet_rows = sheet_bar_summary(OUT / "cross_file")
    total_sheet_bars = sum(b for _, b in sheet_rows)

    print()
    print("=== 35A1-JC1 全册交付 ===")
    print(f"deliver ok: {pd.get('ok')}")
    print(f"deliver status: {pd.get('status')}")
    print(f"空间 merge_stems: {pd.get('merge_report', {}).get('merge_stems')}")
    print(f"全册有杆件的分册: {len(sheet_rows)} 张，合计 {total_sheet_bars} 根（各分册独立解析）")
    print(f"3D 合并模型杆件: {stats['bars']}")
    print(f"3D 节点: {stats['nodes']}")
    print(f"塔高 bbox Z: {stats.get('bbox_mm', {}).get('z', '未解算')}")
    print(f"门禁: {'通过' if gate['ok'] else '失败'}")
    if gate.get("reasons"):
        for r in gate["reasons"]:
            print(f"  - {r}")

    if GT_PATH.exists():
        bom_file = REPO / "examples/external/guowang_35A1/guowang_merged_bom.csv"
        ev_cmd = [
            sys.executable, str(REPO / "scripts/evaluate_ground_truth.py"),
            str(GT_PATH), str(OUT / "model.json"), "--view", "front",
        ]
        if bom_file.exists():
            ev_cmd.extend(["--bom", str(bom_file)])
        ev = subprocess.run(
            ev_cmd,
            capture_output=True, text=True,
        )
        print()
        print("=== GT 评测（3D 合并模型）===")
        for ln in ev.stdout.strip().splitlines():
            print(ln)

    # Phase A3：skeleton.glb 是 M3 骨架主产物（P4 已删除 tower.glb 兼容别名）。
    skeleton = OUT / "skeleton.glb"
    glb = skeleton if skeleton.exists() else None
    if glb and glb.exists():
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(glb, DEMO_DIR / "tower_from_dxf.glb")
        print(f"\nGLB(M3 skeleton): {glb} ({glb.stat().st_size // 1024} KB)")
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

    # P0-2 失败传播：verified → 0，review_required → 1，failed → 2。
    status = pd.get("status", "failed")
    exit_code = {"verified": 0, "review_required": 1, "failed": 2}.get(status, 2)
    return exit_code


if __name__ == "__main__":
    import traceback
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        # 任何未捕获异常都落盘 traceback，避免「无声退出、log 为空」。
        print(tb, flush=True)
        try:
            (OUT / "crash_traceback.log").write_text(tb, encoding="utf-8")
            print(f"崩溃 traceback 已写: {OUT / 'crash_traceback.log'}", flush=True)
        except Exception:
            pass
        raise
