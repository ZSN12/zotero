# -*- coding: utf-8 -*-
"""P0 修复回归：评测口径 key 存在性（2026-09-05）。

背景（审计发现）：eval_a2_multi_caliber 自 P1 五层口径重构（94c7fad）
起返回 caliber key "pure"，而 scripts/eval_a2_profiles.py 曾查
"pure_dxf"（该 key 只存在于 eval_a2_dual_caliber 顶层）→ A2-front-pure
恒 0、observability.multi_view_tp_gain_vs_front_pure 系统性夸大；
scripts/diff_tp_regression.py 同款问题导致 pure 层回归 diff 恒空。

本测试锁定：
1. eval_a2_multi_caliber 返回的 calibers 必含 "pure"（五层口径名），
   不再出现 "pure_dxf"（避免调用方用旧 key 静默拿到空集）；
2. eval_a2_dual_caliber 顶层必含 "pure_dxf"（历史名，仍被
   eval_a2_profiles L91 引用）；
3. eval_a2_profiles.py 源码对 multi caliber 的取值使用 "pure" 优先
   的双 key 兼容（防再次断链）。
"""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    eval_a2_multi_caliber,
    eval_a2_dual_caliber,
)


def _gt_fixture():
    return {
        "nodes": {
            "A": [-500.0, 0.0, 0.0], "B": [500.0, 0.0, 0.0],
            "C": [-450.0, 0.0, 1000.0], "D": [450.0, 0.0, 1000.0],
        },
        "bars": [
            {"id": "GT1", "from": "A", "to": "C", "section": "L56X4"},
            {"id": "GT2", "from": "A", "to": "B", "section": "L40X3"},
        ],
    }


def _model_fixture():
    """1 根 dxf_geom 直读杆（recognized/pure 池）+ 1 根派生杆。"""
    return {
        "components": {
            "nA": {"kind": "tower_node", "properties": {"x": -500.0, "y": 0.0, "z": 0.0, "node_id": "nA"}},
            "nC": {"kind": "tower_node", "properties": {"x": -450.0, "y": 0.0, "z": 1000.0, "node_id": "nC"}},
            "nB": {"kind": "tower_node", "properties": {"x": 500.0, "y": 0.0, "z": 0.0, "node_id": "nB"}},
            "b1": {
                "kind": "tower_bar",
                "properties": {
                    "from_node": "nA", "to_node": "nC",
                    "geometry_origin": "dxf_geom",
                    "geometry_class": "recognized",
                },
            },
            "b2": {
                "kind": "tower_bar",
                "properties": {
                    "from_node": "nA", "to_node": "nB",
                    "geometry_origin": "panel_template_completion",
                    "geometry_class": "derived_parametric",
                },
            },
        }
    }


class TestCaliberKeyContract(unittest.TestCase):
    def test_multi_caliber_has_pure_key(self):
        cal = eval_a2_multi_caliber(
            _gt_fixture(), _model_fixture(), view="front", tols=(500.0,))
        keys = set((cal.get("calibers") or {}).keys())
        self.assertIn("pure", keys, "multi caliber 必须含 pure 层（五层口径）")
        self.assertNotIn(
            "pure_dxf", keys,
            "multi caliber 不应出现 pure_dxf（该 key 属 dual_caliber），"
            "存在会掩盖调用方旧 key 断链")

    def test_dual_caliber_has_pure_dxf_key(self):
        cal = eval_a2_dual_caliber(
            _gt_fixture(), _model_fixture(), view="front", tols=(500.0,))
        self.assertIn(
            "pure_dxf", cal,
            "dual_caliber 顶层必须保留 pure_dxf（eval_a2_profiles L91 引用）")

    def test_profiles_script_uses_pure_first(self):
        """eval_a2_profiles.py 对 multi caliber 取 pure 优先（双 key 兼容）。"""
        src = (REPO / "scripts" / "eval_a2_profiles.py").read_text(
            encoding="utf-8")
        self.assertIn(
            'cal.get("pure") or cal.get("pure_dxf")', src,
            "eval_a2_profiles 必须先取 multi caliber 的 pure（新 key），"
            "再回退 pure_dxf——防口径断链复发")
        # diff_tp_regression 同款双 key 兼容
        src2 = (REPO / "scripts" / "diff_tp_regression.py").read_text(
            encoding="utf-8")
        self.assertIn(
            '_pick_sweep(base_eval, "pure")', src2,
            "diff_tp_regression 必须先取 pure（新 key）再回退 pure_dxf")


if __name__ == "__main__":
    unittest.main()
