#!/usr/bin/env python3
"""对比两份 model.json 的 A2 full TP 回归：定位 baseline 命中、compare 丢失的 GT 杆。

用法：
    python3 scripts/diff_tp_regression.py \\
        --baseline out/ab/none/model.json \\
        --compare out/ab/p11/model.json \\
        --gt examples/gt/35A1-JC1_ground_truth.json \\
        --json-out out/ab/tp_regression_diff.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    DEFAULT_TOLS,
    eval_a2_multi_caliber,
    gt_bars_2d,
)
from traceability.eval.generation_status import collect_generation_status  # noqa: E402


def _matched_gt_ids(eval_result: dict, tol: float = 500.0) -> Set[str]:
    full = (eval_result.get("calibers") or {}).get("full") or {}
    rows = full.get("by_tolerance") or full.get("sweep") or []
    matched_pairs = []
    for row in rows:
        if abs(float(row.get("tolerance_mm", row.get("tol", 0))) - tol) < 1e-6:
            matched_pairs = eval_result.get("match_provenance") or []
            break
    if not matched_pairs:
        matched_pairs = eval_result.get("match_provenance") or []
    out: Set[str] = set()
    for rec in matched_pairs:
        if rec.get("match_status") == "tp" and rec.get("gt_bar_id"):
            out.add(str(rec["gt_bar_id"]))
    return out


def _gt_meta(gt: dict, view: str = "front") -> Dict[str, dict]:
    bars_by_id = {str(b.get("id")): b for b in (gt.get("bars") or [])}
    g = gt_bars_2d(gt, view)
    nodes = gt.get("nodes") or {}
    meta: Dict[str, dict] = {}
    for _seg, gid, _sec in g:
        bar = bars_by_id.get(str(gid), {})
        fa = nodes.get(str(bar.get("from") or ""))
        fb = nodes.get(str(bar.get("to") or ""))
        z_mid = None
        if fa and fb:
            z_mid = (float(fa[2]) + float(fb[2])) / 2.0
        meta[str(gid)] = {
            "role": bar.get("role") or bar.get("member_type"),
            "z_mid_mm": round(z_mid, 1) if z_mid is not None else None,
        }
    return meta


def diff_tp_regression(
    *,
    gt: dict,
    baseline_model: dict,
    compare_model: dict,
    tol: float = 500.0,
    view: str = "front",
) -> dict:
    base_eval = eval_a2_multi_caliber(gt, baseline_model, view=view, tols=(tol,))
    cmp_eval = eval_a2_multi_caliber(gt, compare_model, view=view, tols=(tol,))

    def _pick_sweep(ev: dict, caliber: str) -> dict:
        cal = (ev.get("calibers") or {}).get(caliber) or {}
        rows = cal.get("by_tolerance") or cal.get("sweep") or []
        return rows[0] if rows else {}

    base_full = _pick_sweep(base_eval, "full")
    cmp_full = _pick_sweep(cmp_eval, "full")
    base_pure = _pick_sweep(base_eval, "pure_dxf")
    cmp_pure = _pick_sweep(cmp_eval, "pure_dxf")

    base_matched = _matched_gt_ids(base_eval, tol)
    cmp_matched = _matched_gt_ids(cmp_eval, tol)
    lost = sorted(base_matched - cmp_matched)
    gained = sorted(cmp_matched - base_matched)

    gt_meta = _gt_meta(gt, view)
    by_role: Counter = Counter()
    by_z_band: Counter = defaultdict(int)
    lost_detail: List[dict] = []
    for gid in lost:
        m = gt_meta.get(gid, {})
        role = str(m.get("role") or "unknown")
        by_role[role] += 1
        z = m.get("z_mid_mm")
        band = "unknown"
        if z is not None:
            band = f"{int(z // 5000) * 5}k-{(int(z // 5000) + 1) * 5}k"
        by_z_band[band] += 1
        lost_detail.append({"gt_bar_id": gid, **m})

    base_gen = collect_generation_status(baseline_model)
    cmp_gen = collect_generation_status(compare_model)

    return {
        "tolerance_mm": tol,
        "view": view,
        "baseline": {
            "pure": base_pure,
            "full": base_full,
            "n_matched_gt": len(base_matched),
        },
        "compare": {
            "pure": cmp_pure,
            "full": cmp_full,
            "n_matched_gt": len(cmp_matched),
        },
        "delta": {
            "pure_tp": int(cmp_pure.get("tp", 0)) - int(base_pure.get("tp", 0)),
            "full_tp": int(cmp_full.get("tp", 0)) - int(base_full.get("tp", 0)),
            "n_lost_gt": len(lost),
            "n_gained_gt": len(gained),
        },
        "lost_gt_by_role": dict(by_role),
        "lost_gt_by_z_band": dict(by_z_band),
        "lost_gt_ids": lost,
        "lost_gt_detail": lost_detail[:200],
        "gained_gt_ids": gained,
        "generation_status_delta": {
            "baseline_diagonal_generated": _safe_gen(base_gen),
            "compare_diagonal_generated": _safe_gen(cmp_gen),
            "delta_generated": _safe_gen(cmp_gen) - _safe_gen(base_gen),
        },
    }


def _safe_gen(status: dict) -> int:
    return int((status.get("diagonal_topology") or {}).get("totals", {}).get("generated", 0))


def main() -> int:
    ap = argparse.ArgumentParser(description="A2 full TP 回归 diff")
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--compare", type=Path, required=True)
    ap.add_argument("--gt", type=Path, default=REPO / "examples/gt/35A1-JC1_ground_truth.json")
    ap.add_argument("--tol", type=float, default=500.0)
    ap.add_argument("--view", default="front")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    gt = json.loads(args.gt.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    compare = json.loads(args.compare.read_text(encoding="utf-8"))

    report = diff_tp_regression(
        gt=gt, baseline_model=baseline, compare_model=compare,
        tol=args.tol, view=args.view,
    )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json_out}", file=sys.stderr)

    delta = report["delta"]["full_tp"]
    if delta < -3:
        print(f"\n⚠ full TP 回归 {delta}（超过 −3 容忍）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
