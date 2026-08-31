#!/usr/bin/env python3
"""全链路 06 selection_mode A/B：none / p11 / relaxed × deliver + A2 指标 + TP diff。

用法：
    python3 scripts/ab_full_pipeline.py \\
        --dxf-dir out/xianyu-acceptance/batch-jc1/dxf

    # 已有产物，仅重评：
    python3 scripts/ab_full_pipeline.py --skip-deliver \\
        --baseline-dir out/ab-selection/none \\
        --compare-dir out/ab-selection/p11

输出：
    out/ab-selection/ab_full_pipeline_report.json
    out/ab-selection/tp_regression_none_vs_p11.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

GT = REPO / "examples/gt/35A1-JC1_ground_truth.json"
OVERLAY = REPO / "examples/external/guowang_35A1/layer_overlay.json"
DEFAULT_DXF = REPO / "out/xianyu-acceptance/batch-jc1/dxf"
DEFAULT_OUT = REPO / "out/ab-selection"

MODES = ("none", "p11", "relaxed")


def _run(cmd: List[str], *, label: str) -> int:
    print(f"\n=== {label} ===")
    print(" ".join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO))
    return r.returncode


def _eval_profiles(model_path: Path, out_json: Path) -> dict:
    rc = _run([
        sys.executable, str(REPO / "scripts/eval_a2_profiles.py"),
        str(GT), str(model_path),
        "--overlay", str(OVERLAY),
        "--json-out", str(out_json),
    ], label=f"eval_a2_profiles ({model_path.parent.name})")
    if rc != 0 or not out_json.exists():
        return {"ok": False}
    return {"ok": True, **json.loads(out_json.read_text(encoding="utf-8"))}


def _write_generation_status(model_path: Path) -> dict:
    from traceability.eval.generation_status import collect_generation_status

    model = json.loads(model_path.read_text(encoding="utf-8"))
    status = collect_generation_status(model)
    out = model_path.parent / "generation_status.json"
    out.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return status


def _deliver_mode(
    *,
    dxf_dir: Path,
    out_dir: Path,
    mode: str,
    agent_mode: str,
    profile: str,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    return _run([
        sys.executable, str(REPO / "scripts/run_35A1_jc1_full.py"),
        "--dxf-dir", str(dxf_dir),
        "--out-dir", str(out_dir),
        "--selection-mode", mode,
        "--profile", profile,
        "--agent-mode", agent_mode,
        "--skip-sync",
    ], label=f"deliver selection_mode={mode}")


def _summarize_row(mode: str, profiles: dict, gen: dict, deliver_ok: bool) -> dict:
    p = profiles.get("profiles") or {}
    front = p.get("A2-front-pure") or {}
    dual = p.get("A2-dual-view-reconstructed") or {}
    dt = gen.get("diagonal_topology") or {}
    totals = dt.get("totals") or {}
    per = dt.get("per_sheet") or []
    sheet_06 = next((s for s in per if "06" in str(s.get("sheet", ""))), {})
    return {
        "mode": mode,
        "deliver_ok": deliver_ok,
        "pure_tp": front.get("TP"),
        "pure_fp": front.get("FP"),
        "dual_reconstructed_tp": dual.get("TP"),
        "dual_reconstructed_r_pct": dual.get("R_pct"),
        "full_tp_note": dual.get("note"),
        "diagonal_generated_total": totals.get("generated"),
        "fan_pairs_total": totals.get("fan_pairs"),
        "sheet_06_generated": sheet_06.get("generated"),
        "sheet_06_rejected": sheet_06.get("selection_rejected"),
        "sheet_06_reject_reasons": sheet_06.get("reject_reasons"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="全链路 selection_mode A/B")
    ap.add_argument("--dxf-dir", type=Path, default=DEFAULT_DXF)
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf")
    ap.add_argument("--profile", choices=["canonical_assisted", "production_dxf"],
                    default="canonical_assisted")
    ap.add_argument("--modes", default=",".join(MODES),
                    help="逗号分隔：none,p11,relaxed")
    ap.add_argument("--skip-deliver", action="store_true")
    ap.add_argument("--baseline-dir", type=Path, default=None,
                    help="skip-deliver 时 baseline（none）目录")
    ap.add_argument("--compare-dir", type=Path, default=None,
                    help="skip-deliver 时 compare（p11）目录")
    args = ap.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if not args.skip_deliver and not args.dxf_dir.exists():
        print(f"DXF 目录不存在: {args.dxf_dir}", file=sys.stderr)
        print("  请准备 batch-jc1/dxf 或传 --dxf-dir", file=sys.stderr)
        return 2
    if not GT.exists():
        print(f"GT 缺失: {GT}", file=sys.stderr)
        return 2

    args.out_root.mkdir(parents=True, exist_ok=True)
    rows: List[dict] = []
    mode_dirs: Dict[str, Path] = {}

    for mode in modes:
        mode_dir = args.out_root / mode
        mode_dirs[mode] = mode_dir
        deliver_ok = True
        if not args.skip_deliver:
            rc = _deliver_mode(
                dxf_dir=args.dxf_dir,
                out_dir=mode_dir,
                mode=mode,
                agent_mode=args.agent_mode,
                profile=args.profile,
            )
            deliver_ok = rc == 0
        model_path = mode_dir / "model.json"
        if not model_path.exists():
            rows.append({"mode": mode, "error": "model.json missing", "deliver_ok": deliver_ok})
            continue
        gen = _write_generation_status(model_path)
        prof_path = mode_dir / "eval_a2_profiles.json"
        prof = _eval_profiles(model_path, prof_path)
        rows.append(_summarize_row(mode, prof, gen, deliver_ok))

    report: Dict[str, Any] = {
        "dxf_dir": str(args.dxf_dir),
        "profile": args.profile,
        "agent_mode": args.agent_mode,
        "modes": modes,
        "rows": rows,
    }

    none_dir = mode_dirs.get("none") or args.baseline_dir
    p11_dir = mode_dirs.get("p11") or args.compare_dir
    if none_dir and p11_dir:
        none_model = none_dir / "model.json"
        p11_model = p11_dir / "model.json"
        if none_model.exists() and p11_model.exists():
            diff_out = args.out_root / "tp_regression_none_vs_p11.json"
            rc = _run([
                sys.executable, str(REPO / "scripts/diff_tp_regression.py"),
                "--baseline", str(none_model),
                "--compare", str(p11_model),
                "--gt", str(GT),
                "--json-out", str(diff_out),
            ], label="TP regression diff (none vs p11)")
            if diff_out.exists():
                report["tp_regression_none_vs_p11"] = json.loads(
                    diff_out.read_text(encoding="utf-8"))
            report["tp_regression_ok"] = rc == 0

    out_path = args.out_root / "ab_full_pipeline_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n## Full pipeline A/B\n")
    print("| mode | deliver | pure TP | pure FP | dual recon TP | dt gen | 06 gen | rejected |")
    print("|------|---------|---------|---------|---------------|--------|--------|----------|")
    for r in rows:
        if r.get("error"):
            print(f"| {r['mode']} | ERR | — | — | — | — | — | — |")
            continue
        print(
            f"| {r['mode']} | {r.get('deliver_ok')} | {r.get('pure_tp')} | "
            f"{r.get('pure_fp')} | {r.get('dual_reconstructed_tp')} | "
            f"{r.get('diagonal_generated_total')} | {r.get('sheet_06_generated')} | "
            f"{r.get('sheet_06_rejected')} |"
        )
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
