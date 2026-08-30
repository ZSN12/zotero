#!/usr/bin/env python3
"""P0 评测重写：Ground Truth 3D 物理评测（Hungarian + tolerance sweep + derived 排除）。

对比完整 3D 模型 vs 3D GT（1071 杆 / 358 节点）。原实现用「中点贪心 + 单一
800mm」，现迁移到 traceability/eval/metrics.py：
    * 一对一 Hungarian 最优匹配
    * derived 构件（整高合成角腿/自动 diaphragm/镜像面）不进 physical P/R
    * tolerance sweep（200/500/800mm）
    * 按杆件类型（leg/diagonal/horizontal）细分召回缺口

用法：
    python3 scripts/evaluate_ground_truth_3d.py <gt.json> <model.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from traceability.eval.metrics import eval_m3_physical_3d, model_has_gt_alignment


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json 路径")
    ap.add_argument("model", help="管线输出 model.json")
    ap.add_argument("--tol", type=float, default=None,
                    help="兼容旧参数；评测改用 tolerance sweep，忽略单点 tol")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    if model_has_gt_alignment(model):
        print("✗ GT 泄漏：模型含 gt_aligned=True 杆件，正式评测拒绝（阶段 0.2）。")
        print("  该模型只能用于调试/误差分析，不得作为正式指标。")
        sys.exit(3)

    result = eval_m3_physical_3d(gt, model)

    print("=== Ground Truth 3D 物理评测（Hungarian 一对一匹配，排除 derived）===")
    print(f"GT 3D 物理杆件: {result['n_gt']}")
    print(f"模型 3D 物理杆件（排除 derived）: {result['n_model']}")
    print()
    print("tolerance sweep：")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10}")
    for s in result["sweep"]:
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%}")

    print("\n按杆件类型的 GT 构成与召回缺口：")
    for t, info in result.get("recall_by_type", {}).items():
        total = info.get("total", 0)
        missed = info.get("missed", 0)
        if total:
            print(f"  {t:12s}: 共 {total:4d} 根，漏检 {missed:4d} 根（召回 {info.get('recall', 0.0):.1%}）")

    # 镜像面分口径（阶段 5.2 审计）：镜像重建面（B/L/R）是合成预测，与仅来自
    # 正立面合并的 GT 对不上时会推高 FP——分面精度必须可见，不静默混入总分。
    by_face = result.get("model_count_by_face") or {}
    if by_face:
        print("\n按生成面分解（模型物理杆件，GT 无面标签故仅输出计数/精度）：")
        prec_face = result.get("precision_by_face", {})
        matched_face = result.get("matched_model_count_by_face", {})
        for f in sorted(by_face):
            print(f"  face {f:8s}: 共 {by_face[f]:4d} 根，匹配 {matched_face.get(f, 0):4d} 根"
                  f"（精度 {prec_face.get(f, 0.0):.1%}）")


if __name__ == "__main__":
    main()
