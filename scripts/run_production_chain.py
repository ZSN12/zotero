#!/usr/bin/env python3
"""Phase 7：生产交付链一键脚本（deliver → postprocess → eval → report）。

串联：
  1. scripts/run_35A1_jc1_full.py（deliver + review_queue + diff + sync）
  2. scripts/run_frozen_eval.py（development 口径 A2 profiles）
  3. scripts/generate_final_report.md（若 GT 存在）

用法：
    python3 scripts/run_production_chain.py
    python3 scripts/run_production_chain.py --profile production_dxf --agent-mode ezdxf
    python3 scripts/run_production_chain.py --skip-deliver --out-dir out/35A1-JC1-full-deliver
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "out/35A1-JC1-full-deliver"
GT = REPO / "examples/gt/35A1-JC1_ground_truth.json"
SPLIT = REPO / "examples/dataset_split.json"


def _run(cmd: list[str], *, label: str) -> int:
    print(f"\n=== {label} ===")
    print(" ".join(cmd))
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode != 0:
        print(f"[FAIL] {label} exit={r.returncode}", file=sys.stderr)
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 7 生产交付链")
    ap.add_argument("--profile", choices=["canonical_assisted", "production_dxf"],
                    default="canonical_assisted")
    ap.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf")
    ap.add_argument("--skip-deliver", action="store_true",
                    help="跳过 deliver，仅 postprocess/eval/report")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="交付目录（默认随 profile 变化）")
    ap.add_argument("--skip-sync", action="store_true",
                    help="跳过 demo 同步（run_35A1 内 postprocess 步骤）")
    args = ap.parse_args()

    out_dir = args.out_dir
    if out_dir is None:
        out_dir = (REPO / "out/35A1-JC1-production"
                   if args.profile == "production_dxf"
                   else DEFAULT_OUT)

    split_label = "development"
    if SPLIT.exists():
        split = json.loads(SPLIT.read_text(encoding="utf-8"))
        split_label = "production_dxf" if args.profile == "production_dxf" else "development"
        print(f"Dataset split label: {split_label}")
        print(f"  policy: {split.get('rules', {}).get('report_labels', '')}")

    rc = 0
    chain_report: dict = {
        "profile": args.profile,
        "agent_mode": args.agent_mode,
        "dataset_split": split_label,
        "out_dir": str(out_dir),
        "steps": {},
    }

    if not args.skip_deliver:
        cmd = [
            sys.executable, str(REPO / "scripts/run_35A1_jc1_full.py"),
            "--profile", args.profile,
            "--agent-mode", args.agent_mode,
        ]
        rc = _run(cmd, label="deliver (run_35A1_jc1_full)")
        chain_report["steps"]["deliver"] = {"ok": rc == 0}
        if rc != 0:
            chain_report["ok"] = False
            _write_chain_report(out_dir, chain_report)
            return rc
    else:
        chain_report["steps"]["deliver"] = {"ok": True, "skipped": True}

    model_path = out_dir / "model.json"
    if model_path.exists() and GT.exists():
        ev_rc = _run([
            sys.executable, str(REPO / "scripts/run_frozen_eval.py"),
            "--model", str(model_path),
        ], label="frozen eval (development)")
        chain_report["steps"]["frozen_eval"] = {"ok": ev_rc == 0}
        rc = rc or ev_rc

        prof_rc = _run([
            sys.executable, str(REPO / "scripts/eval_a2_profiles.py"),
            str(GT), str(model_path),
            "--json-out", str(out_dir / "eval_a2_profiles.json"),
        ], label="A2 profiles eval")
        chain_report["steps"]["a2_profiles"] = {"ok": prof_rc == 0}
        rc = rc or prof_rc
    else:
        chain_report["steps"]["frozen_eval"] = {
            "ok": False,
            "skipped": True,
            "reason": "model or GT missing",
        }

    if model_path.exists() and GT.exists():
        fr_rc = _run([
            sys.executable, str(REPO / "scripts/generate_final_report.py"),
        ], label="final report")
        chain_report["steps"]["final_report"] = {"ok": fr_rc == 0}
        rc = rc or fr_rc

    chain_report["ok"] = all(
        s.get("ok", False) or s.get("skipped")
        for s in chain_report["steps"].values()
    )
    _write_chain_report(out_dir, chain_report)
    print(f"\nChain report → {out_dir / 'production_chain_report.json'}")
    return rc


def _write_chain_report(out_dir: Path, report: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "production_chain_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
