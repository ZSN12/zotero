#!/usr/bin/env python3
"""P4：冻结参数评测入口（development / 未来 blind_test）。

用法：
    python3 scripts/run_frozen_eval.py --model out/.../model.json
    python3 scripts/run_frozen_eval.py --check-overlay examples/external/guowang_35A1/layer_overlay.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FROZEN = REPO / "profiles/frozen_jc1_development.json"
GT = REPO / "examples/gt/35A1-JC1_ground_truth.json"


def check_overlay(overlay_path: Path, frozen: dict) -> list[str]:
    ov = json.loads(overlay_path.read_text(encoding="utf-8"))
    keys = frozen.get("overlay_keys") or {}
    drift: list[str] = []
    for k, expected in keys.items():
        actual = ov.get(k)
        if actual != expected:
            drift.append(f"{k}: expected={expected!r} actual={actual!r}")
    return drift


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, default=None)
    ap.add_argument("--check-overlay", type=Path, default=None)
    ap.add_argument("--frozen", type=Path, default=FROZEN)
    args = ap.parse_args()

    frozen = json.loads(args.frozen.read_text(encoding="utf-8"))
    print(f"Frozen profile: {args.frozen.name} ({frozen.get('dataset_split')})")

    if args.check_overlay:
        drift = check_overlay(args.check_overlay, frozen)
        if drift:
            print("Overlay drift vs frozen:")
            for d in drift:
                print(f"  - {d}")
            return 1
        print("Overlay matches frozen keys.")
        return 0

    if not args.model or not args.model.exists():
        print("Provide --model or --check-overlay", file=sys.stderr)
        return 2
    if not GT.exists():
        print(f"GT missing: {GT}", file=sys.stderr)
        return 2

    import subprocess
    cmd = [
        sys.executable, str(REPO / "scripts/eval_a2_profiles.py"),
        str(GT), str(args.model),
        "--json-out", str(args.model.parent / "eval_a2_profiles.json"),
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
