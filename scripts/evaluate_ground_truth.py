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
    model_has_gt_alignment,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt", help="GT json 路径")
    ap.add_argument("model", help="管线输出 model.json")
    ap.add_argument("--view", choices=["front", "side"], default="front")
    ap.add_argument("--tol", type=float, default=None,
                    help="兼容旧参数；评测改用 tolerance sweep，忽略单点 tol")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))

    if model_has_gt_alignment(model):
        print("✗ GT 泄漏：模型含 gt_aligned=True 杆件，正式评测拒绝（阶段 0.2）。")
        print("  该模型只能用于调试/误差分析，不得作为正式指标。")
        sys.exit(3)

    result = eval_a2_geometry_2d(gt, model, view=args.view, tols=DEFAULT_TOLS)

    print(f"=== Ground Truth 2D 评测（{args.view} 投影，Hungarian 一对一匹配）===")
    print(f"GT 投影杆件（去重后）: {result['n_gt']}")
    print(f"模型物理杆件（排除 derived）: {result['n_model']}")
    print()
    print("tolerance sweep：")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10}")
    for s in result["sweep"]:
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%}")

    # 件号 Exact Match（匹配对中，A1 标签 + A3 关联产物）
    lem = result.get("label_exact_match", {})
    if lem.get("matched"):
        print(f"\n件号 Exact Match（匹配对中）: {lem['exact']}/{lem['matched']} = {lem['rate']:.1%}")
    else:
        print("\n件号 Exact Match: 无匹配对")

    # A1 件号识别（独立口径：GT 件号集合 vs 模型识别件号集合）
    a1 = eval_a1_labels(gt, model)
    print(f"\n=== A1 件号识别（独立于几何匹配）===")
    print(f"GT 件号: {a1['n_gt']}，模型识别件号: {a1['n_model']}，"
          f"Exact Match: {a1['tp']}（P={a1['precision']:.1%} R={a1['recall']:.1%}）")


if __name__ == "__main__":
    main()
