#!/usr/bin/env python3
"""P0 评测重写：Ground Truth 2D 投影评测（Hungarian + tolerance sweep + derived 排除）。

原实现用「中点贪心匹配 + 单一容差」，现迁移到 traceability/eval/metrics.py：
    * 一对一 Hungarian 最优匹配（scipy.linear_sum_assignment）
    * 代价含双端点距离 + 角度 + 长度比 + 共线重叠
    * tolerance sweep（50/100/200/500mm）
    * derived 构件（镜像面/corner_leg/diaphragm）不进 physical P/R

A2 几何检测（front 投影）是四套指标之一，与 A1 标签 / A3 关联 / M3 物理 3D
各自独立，不在本脚本混算。

用法：
    python3 scripts/evaluate_ground_truth.py <gt.json> <model.json> [--view front]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from traceability.eval.metrics import (
    DEFAULT_TOLS,
    eval_a2_geometry_2d,
    eval_a2_dual_caliber,
    eval_a2_multi_caliber,
    eval_a1_labels,
    eval_a3_association,
    model_has_gt_alignment,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json 路径")
    ap.add_argument("model", help="管线输出 model.json")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--bom", default=None,
                    help="master BOM csv（含 bar_id 列），A1 的图纸件号 GT 基准")
    ap.add_argument("--allow-legacy-semantics", action="store_true",
                    help="兼容旧模型 evidence_status 语义（正式评测应禁用，默认 fail-closed）")
    ap.add_argument("--tol", type=float, default=None,
                    help="兼容旧参数；评测改用 tolerance sweep，忽略单点 tol")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    if model_has_gt_alignment(model):
        print("✗ GT 泄漏：模型含 gt_aligned=True 杆件，正式评测拒绝（阶段 0.2）。")
        print("  该模型只能用于调试/误差分析，不得作为正式指标。")
        sys.exit(3)

    # 阶段2.1：A1 的 GT 件号基准 = 图纸/BOM 可见件号（非物理 ID PM_XXXX）
    gt_label_ids = None
    id_mapping = None
    if args.bom and Path(args.bom).exists():
        import csv as _csv
        from traceability.project.bar_id_mapping import build_bar_id_mapping
        _bom_rows = list(_csv.DictReader(
            Path(args.bom).read_text(encoding="utf-8-sig").splitlines()))
        gt_label_ids = {str(r.get("bar_id", "")).strip() for r in _bom_rows
                        if str(r.get("bar_id", "")).strip()}
        _map_res = build_bar_id_mapping(gt, _bom_rows)
        # 支持多对多集合映射
        id_mapping = {}
        for bid, d in _map_res.get("mapping", {}).items():
            gids = d.get("gt_ids", [])
            if gids:
                id_mapping[bid] = gids

    result = eval_a2_geometry_2d(gt, model, view=args.view, tols=DEFAULT_TOLS,
                                 allow_legacy=args.allow_legacy_semantics)

    print(f"=== A2 几何检测（{args.view} 投影，Hungarian 一对一匹配）===")
    print(f"GT 投影杆件（去重后）: {result['n_gt']}")
    print(f"模型物理杆件（排除 derived）: {result['n_model']}")

    # P0 口径诚实化：physical 口径含「用 GT canonical 标高重建的横隔/节间」，
    # 属借助 GT 的增强成分。对外汇报必须以纯 DXF 口径为主口径，辅助增量单列，
    # 否则等于把抄答案的贡献算成图纸→几何的识别能力。
    dual = eval_a2_dual_caliber(gt, model, view=args.view, tols=DEFAULT_TOLS,
                                allow_legacy=args.allow_legacy_semantics)
    print()
    print("【主口径】A2-pure（纯 DXF 识别，排除 GT 标高辅助）——对外可汇报的真实能力")
    print(f"模型杆件: {dual['n_model_pure']}（直接识别）")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10}")
    for s in dual["pure_dxf"]["sweep"]:
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%}")

    print()
    print("【增强口径】A2-full（physical，含 GT 标高辅助重建）——仅内部归因用")
    print(f"模型杆件: {dual['n_model_full']}（含辅助 {dual['assisted']}）")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10} "
          f"{'其中辅助增量':>12}")
    for s in dual["full"]["sweep"]:
        gain = next((g["assisted_gain"] for g in dual["assisted_gain"]
                     if g["tol"] == s["tol"]), 0)
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%} {gain:>12d}")

    cl = dual["ceiling"]
    print()
    print(f"【口径上限】{args.view} 2D 理论天花板 {cl['ceiling_rate']:.1%} "
          f"（{cl['ceiling']}/{cl['n_gt']}），超出部分属投影不可达：")
    print(f"  - y_member {cl['y_member_unmeasurable']} 根：{cl['reason']['y_member']}")
    print(f"  - depth_diag 重合损失 {cl['depth_diag_overlap_loss']} 根：{cl['reason']['depth_diag']}")
    print()
    print("tolerance sweep（physical 口径，兼容旧输出）：")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10}")
    for s in result["sweep"]:
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%}")

    # 任务 5（P3）：A2-effective 有效高度口径（底段 z<6500 无图纸来源，
    # 客观源缺失不应算进识别能力的分母；双口径并列，全高口径仍为正式指标）
    eff = result.get("effective")
    if eff:
        print(f"\nA2-effective（z >= {eff['z_min_mm']:.0f}mm，双侧同口径，剔除 GT 无源杆 {eff['gt_excluded']} 根）：")
        for s in eff["sweep"]:
            print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
                  f"{s['precision']:>10.1%} {s['recall']:>10.1%}")

    # A3 关联评测（几何匹配对中的件号是否正确关联）
    a3 = eval_a3_association(gt, model, view=args.view, id_mapping=id_mapping,
                             allow_legacy=args.allow_legacy_semantics)
    print(f"\n=== A3 件号关联（几何匹配对中）===")
    print(f"匹配对: {a3['matched_pairs']}，正确关联: {a3['correct_association']}，"
          f"关联率: {a3['association_rate']:.1%}")

    # 件号 Exact Match（匹配对中，A1 标签 + A3 关联产物）
    lem = result.get("label_exact_match", {})
    if lem.get("matched"):
        print(f"\n件号 Exact Match（匹配对中）: {lem['exact']}/{lem['matched']} = {lem['rate']:.1%}")
    else:
        print("\n件号 Exact Match: 无匹配对")

    # A1 件号识别（独立口径：图纸/BOM 件号集合 vs 模型识别件号集合）
    a1 = eval_a1_labels(gt, model, gt_label_ids=gt_label_ids)
    print(f"\n=== A1 件号识别（独立于几何匹配）===")
    if gt_label_ids is not None:
        print(f"图纸件号（BOM）: {a1['n_gt']}，模型识别件号: {a1['n_model']}，"
              f"Exact Match: {a1['tp']}（P={a1['precision']:.1%} R={a1['recall']:.1%}）")
    else:
        print(f"GT 件号: {a1['n_gt']}，模型识别件号: {a1['n_model']}，"
              f"Exact Match: {a1['tp']}（P={a1['precision']:.1%} R={a1['recall']:.1%}）")

    # ------------------------------------------------------------- #
    # Phase 1（P1.1/P1.2/P1.3）：多口径 + 追溯 + 分角色 + 落盘产物
    # ------------------------------------------------------------- #
    multi = eval_a2_multi_caliber(gt, model, view=args.view, tols=DEFAULT_TOLS,
                                  allow_legacy=args.allow_legacy_semantics)
    print(f"\n=== A2 五层口径并列（Phase 1，默认 tol={DEFAULT_TOLS[-1]:.0f}mm）===")
    print(f"{'口径':<16} {'模型杆':>6} {'TP':>5} {'FP':>5} {'FN':>5} {'P':>8} {'R':>8}")
    for name in ("pure", "reconstructed", "level_assisted", "parametric", "full"):
        cal = multi["calibers"][name]
        s = cal["sweep"][-1]
        print(f"{name:<16} {cal['n_model']:>6} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>8.1%} {s['recall']:>8.1%}")
    print("\n分角色（GT 侧，默认 tol）:")
    for role, rs in multi["by_role"].items():
        print(f"  {role:<12} n_gt={rs['n_gt']:>4}  TP={rs['tp']:>4}  "
              f"FN={rs['fn']:>4}  R={rs['recall']:.1%}")
    print("分来源（模型侧，默认 tol）:")
    for origin, os_ in multi["by_origin"].items():
        print(f"  {origin:<16} n_model={os_['n_model']:>4}  TP={os_['tp']:>4}  FP={os_['fp']:>4}")

    # 计划「五、交付级报告」：metrics 系列产物落盘（与 model.json 同目录）
    out_dir = Path(args.model).parent
    _dump_json(out_dir / "metrics_multi_caliber.json", {
        "calibers": {k: {"n_model": v["n_model"], "sweep": v["sweep"],
                          "metric_scope": v["metric_scope"]}
                     for k, v in multi["calibers"].items()},
        "effective": multi["effective"],
        "ceiling": multi["ceiling"],
        "n_gt": multi["n_gt"],
    })
    _dump_json(out_dir / "metrics_by_role.json", multi["by_role"])
    _dump_json(out_dir / "metrics_by_origin.json", multi["by_origin"])
    _dump_json(out_dir / "evidence_report.json", {
        "description": "每个匹配对/FP 的来源追溯（Phase 1 P1.1，默认 tol）",
        "match_provenance": multi["match_provenance"],
        "counts": {
            "tp": sum(1 for r in multi["match_provenance"] if r["match_status"] == "tp"),
            "fp": sum(1 for r in multi["match_provenance"] if r["match_status"] == "fp"),
        },
    })


def _dump_json(path: Path, obj) -> None:
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"⚠ 指标落盘失败 {path.name}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
