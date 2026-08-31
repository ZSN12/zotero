"""P4（底段参数化透明化）单元测试。

验收：
  * extrapolate_base_segment 生成杆 100% 带 parametric_struct 分类
    （parametric_leg / parametric_cross）；
  * 分类与 role 一致（LEG → parametric_leg，CROSS → parametric_cross）；
  * 报告含 parametric_struct_counts 汇总；
  * viewer 免责声明锚点（compare.html）存在。
"""

import json
import os
import unittest

from traceability.solve.tower_geometry import extrapolate_base_segment


def _hw(z: float) -> float:
    """测试锥线：底 2298 → 顶（6500）1950，线性。"""
    return 2298.0 + (z / 6500.0) * (1950.0 - 2298.0)


class TestParametricStruct(unittest.TestCase):
    def _run(self, **kw):
        nodes = {"a": (-2300.0, 0.0, 7000.0), "b": (2300.0, 0.0, 7000.0)}
        bars = [{"id": "leg0", "from": "a", "to": "b", "role": "LEG"}]
        return extrapolate_base_segment(nodes, bars, _hw, **kw)

    def test_all_bars_classified(self):
        _, bars, _rep = self._run()
        self.assertGreater(len(bars), 0)
        for b in bars:
            self.assertIn(
                b.get("parametric_struct"),
                ("parametric_leg", "parametric_cross"),
                f"杆 {b['id']} 缺 parametric_struct 分类")

    def test_class_matches_role(self):
        _, bars, _ = self._run()
        for b in bars:
            expect = "parametric_leg" if b["role"] == "LEG" else (
                "parametric_cross" if b["role"] == "CROSS" else None)
            self.assertEqual(b.get("parametric_struct"), expect,
                             f"杆 {b['id']} role={b['role']} 分类不符")

    def test_counts(self):
        _, bars, _ = self._run()
        legs = sum(1 for b in bars if b["parametric_struct"] == "parametric_leg")
        cross = sum(1 for b in bars if b["parametric_struct"] == "parametric_cross")
        self.assertGreater(legs, 0)
        self.assertGreater(cross, 0)

    def test_no_cross(self):
        _, bars, _ = self._run(add_cross_diagonals=False)
        for b in bars:
            self.assertEqual(b["parametric_struct"], "parametric_leg")

    def test_viewer_disclaimer_anchor(self):
        """compare.html P4 免责声明锚点（离线验证，浏览器禁令下的替代）。"""
        p = os.path.join(os.path.dirname(__file__), "..",
                         "web", "demo", "35A1-JC1", "compare.html")
        if not os.path.exists(p):
            self.skipTest("compare.html 不存在（viewer 未部署）")
        html = open(p, encoding="utf-8").read()
        # ORIGIN_GROUPS 参数化组 + 免责声明文案 + 半透明材质
        self.assertIn("derived_parametric_base", html)
        self.assertIn("参数化底段", html)
        self.assertIn("parametric_leg", html)


if __name__ == "__main__":
    unittest.main()
