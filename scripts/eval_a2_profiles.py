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
    front_view_ceiling,
)


def _sha(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.exists():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _pick_tol(result: dict, tol: float = 500.0) -> dict:
    rows = result.get("by_tolerance") or result.get("sweep") or []
    for row in rows:
        t = row.get("tolerance_mm", row.get("tol", 0))
        if abs(float(t) - tol) < 1e-6:
            return row
    return rows[-1] if rows else {}


def _resolve_overlay(model_path: Path, cli_value: Optional[str]) -> Optional[Path]:
    """Bug F（P1，2026-09-03）：overlay 指纹反查。

    旧默认写死 guowang_35A1/layer_overlay.json——ZC1 等其它塔的
    a2_dual_view.json 记录的是 JC1 的 overlay 指纹，与该塔 version.json
    的 overlay_sha 恒不一致，对外主口径的复核链第一个就复核不上。

    解析优先级：
      1. CLI 显式 --overlay（信任用户）；
      2. model.json 同目录 version.json 的 overlay_path（生成该模型时
         实际使用的 overlay，权威）；
      3. 都取不到 → 返回 None 并 stderr 提示（overlay_sha256=null，
         复核方能立刻看出指纹缺失，而非拿着错的指纹通过复核）。
    反查到的 overlay 文件不存在时同样 None（指纹链断点显式化）。
    """
    if cli_value:
        p = Path(cli_value)
        return p if p.exists() else None
    version_path = model_path.parent / "version.json"
    if version_path.exists():
        try:
            v = json.loads(version_path.read_text(encoding="utf-8"))
            op = v.get("overlay_path")
            if op:
                p = Path(op)
                if not p.is_absolute():
                    # version.json 记录的是仓库相对路径
                    p = REPO / p
                if p.exists():
                    return p
                print(f"[Bug F] version.json overlay_path 指向不存在的文件: {op}",
                      file=sys.stderr)
        except Exception as exc:
            print(f"[Bug F] version.json 解析失败: {exc}", file=sys.stderr)
    print("[Bug F] 未能反查 overlay（无 --overlay，version.json 无 overlay_path）"
          "——overlay_sha256 将记为 null", file=sys.stderr)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gt")
    ap.add_argument("model")
    ap.add_argument("--overlay", default=None,
                    help="overlay 路径；缺省从 model 同目录 version.json 的 "
                         "overlay_path 反查（Bug F：不再写死 JC1 overlay）")
    ap.add_argument("--tol", type=float, default=500.0)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    gt_path = Path(args.gt)
    model_path = Path(args.model)
    overlay_path = _resolve_overlay(model_path, args.overlay)

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
    ceiling = front_view_ceiling(gt)
    cal = multi.get("calibers") or {}

    # P0 修复（2026-09-05）：eval_a2_multi_caliber 自 P1 五层口径重构
    # （94c7fad）起返回 key "pure"，旧名 "pure_dxf" 仅存在于
    # eval_a2_dual_caliber 的返回。两处兼容读取，任一命中即可——
    # 否则 A2-front-pure 恒 0，multi_view_tp_gain_vs_front_pure 被
    # 系统性夸大（1048−0 而非 1048−131）。
    front_pure = _pick_tol(
        cal.get("pure") or cal.get("pure_dxf") or {}, args.tol)
    full_front = _pick_tol(cal.get("full") or {}, args.tol)
    dual_pure_row = (dual_cal.get("pure_dxf") or {}).get("sweep") or []
    dual_pure = dual_pure_row[0] if dual_pure_row else {}
    # P2.5（2026-09-04 双视 pure 口径修正）：A2-dual-view-pure 此前取
    # eval_a2_dual_caliber 的 front-only pure——名字带 dual 实则单视，
    # 系统性低估「任一立面直读」能力（l/r 面斜材只在 side 剖面图绘制，
    # front 结构性不可见）。改为 eval_a2_dual_view 的 pure 层（front∪
    # side 杆粒度并集，与 A2-dual-view-reconstructed 同构同纪律：
    # 仅 recognized 类入池，GT 杆任一视图匹配即召回，cid 去重计 FP）。
    # 实测（legsynth11）：80→111 TP（+31，全部来自 side 直读的 l/r 面
    # 杆），P 24.8%→32.5%（并集后 cid 去重 FP 同步下降）。
    dual_pure_union_row = (((dual_view.get("calibers") or {}).get("pure") or {})
                           .get("sweep") or [])
    dual_pure_union = dual_pure_union_row[0] if dual_pure_union_row else {}
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
            "TP": dual_pure_union.get("tp", 0),
            "FP": dual_pure_union.get("fp", 0),
            "P_pct": round(100 * float(dual_pure_union.get("precision", 0)), 1),
            "R_pct": round(100 * float(dual_pure_union.get("recall", 0)), 1),
            "note": "front∪side 杆粒度并集、仅 recognized 入池（对外主口径）",
        },
        "A2-dual-view-reconstructed": {
            "TP": dual_full.get("tp", 0),
            "FP": dual_full.get("fp", 0),
            "P_pct": round(100 * float(dual_full.get("precision", 0)), 1),
            "R_pct": round(100 * float(dual_full.get("recall", 0)), 1),
            "note": "full 池含 level-assisted；仅内部归因，不得对外作 pure 能力",
        },
        "A2-front-full": {
            "TP": full_front.get("tp", 0),
            "FP": full_front.get("fp", 0),
            "P_pct": round(100 * float(full_front.get("precision", 0)), 1),
            "R_pct": round(100 * float(full_front.get("recall", 0)), 1),
            "note": "front 单视图 full 池（含 level-assisted），内部归因",
        },
    }

    observability = {
        "front_only_unobservable": int(ceiling.get("y_member_unmeasurable", 0))
            + int(ceiling.get("depth_diag_overlap_loss", 0)),
        "front_ceiling_rate_pct": round(100 * float(ceiling.get("ceiling_rate", 0)), 1),
        "y_member_unmeasurable": ceiling.get("y_member_unmeasurable", 0),
        "depth_diag_overlap_loss": ceiling.get("depth_diag_overlap_loss", 0),
        "multi_view_tp_gain_vs_front_pure": int(dual_full.get("tp", 0))
            - int(front_pure.get("tp", 0)),
    }

    out = {"eval_binding": binding, "profiles": profiles, "observability": observability}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json_out}", file=sys.stderr)
    # Bug E（2026-09-03，P2）：双视口径默认落盘到 model 同目录
    # a2_dual_view.json——version.json 的 a2_dual_view 块从这里并入，
    # 对外主口径（A2-dual-view-pure）与辅助口径（A2-dual-view-
    # reconstructed）从此可从交付产物复核，不再只在 stdout。
    dual_artifact = model_path.parent / "a2_dual_view.json"
    try:
        dual_artifact.write_text(
            json.dumps({
                "eval_binding": binding,
                "profiles": profiles,
                "observability": observability,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {dual_artifact}", file=sys.stderr)
    except OSError as exc:
        print(f"warn: a2_dual_view.json 落盘失败（{exc}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
