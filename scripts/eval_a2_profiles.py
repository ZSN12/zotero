#!/usr/bin/env python3
"""Phase 1：A2 口径 formalize + SHA 绑定（P0.6 / 8-phase Phase 1）。

输出三套 headline KPI（development 标注）：
  * A2-front-pure
  * A2-dual-view-pure
  * A2-dual-view-reconstructed（full 池，含 level-assisted 标注）

用法：
    python3 scripts/eval_a2_profiles.py GT.json model.json [--overlay overlay.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    COST_SEMANTICS,
    eval_a2_dual_caliber,
    eval_a2_dual_view,
    eval_a2_multi_caliber,
)


def _sha(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _pick_tol(result: dict, tol: float = 500.0) -> dict:
    for row in result.get("by_tolerance") or []:
        if abs(float(row.get("tolerance_mm", 0)) - tol) < 1e-6:
            return row
    rows = result.get("by_tolerance") or []
    return rows[-1] if rows else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gt")
    ap.add_argument("model")
    ap.add_argument("--overlay", default=str(REPO / "examples/external/guowang_35A1/layer_overlay.json"))
    ap.add_argument("--tol", type=float, default=500.0)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    gt_path = Path(args.gt)
    model_path = Path(args.model)
    overlay_path = Path(args.overlay) if args.overlay else None

    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    model = json.loads(model_path.read_text(encoding="utf-8"))

    binding = {
        "dataset_split": "development",
        "gt_sha256": _sha(gt_path),
        "model_sha256": _sha(model_path),
        "overlay_sha256": _sha(overlay_path),
        "cost_semantics": COST_SEMANTICS,
        "tolerance_mm": args.tol,
        "view_profiles": ["front_pure", "dual_pure", "dual_full"],
    }

    multi = eval_a2_multi_caliber(gt, model, view="front", tols=(args.tol,))
    dual_cal = eval_a2_dual_caliber(gt, model, view="front", tols=(args.tol,))
    dual_view = eval_a2_dual_view(gt, model, tols=(args.tol,))

    front_pure = _pick_tol(multi.get("pure_dxf") or {}, args.tol)
    dual_pure_row = (dual_cal.get("pure_dxf") or {}).get("sweep") or []
    dual_pure = dual_pure_row[0] if dual_pure_row else {}
    full_sweep = ((dual_view.get("calibers") or {}).get("full") or {}).get("sweep") or []
    dual_full = full_sweep[0] if full_sweep else {}

    profiles = {
        "A2-front-pure": {
            "TP": front_pure.get("tp", 0),
            "FP": front_pure.get("fp", 0),
            "P_pct": round(100 * float(front_pure.get("precision", 0)), 1),
            "R_pct": round(100 * float(front_pure.get("recall", 0)), 1),
        },
        "A2-dual-view-pure": {
            "TP": dual_pure.get("tp", 0),
            "FP": dual_pure.get("fp", 0),
            "P_pct": round(100 * float(dual_pure.get("precision", 0)), 1),
            "R_pct": round(100 * float(dual_pure.get("recall", 0)), 1),
        },
        "A2-dual-view-reconstructed": {
            "TP": dual_full.get("tp", 0),
            "FP": dual_full.get("fp", 0),
            "P_pct": round(100 * float(dual_full.get("precision", 0)), 1),
            "R_pct": round(100 * float(dual_full.get("recall", 0)), 1),
            "note": "full 池含 level-assisted；仅内部归因，不得对外作 pure 能力",
        },
    }

    out = {"eval_binding": binding, "profiles": profiles}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
