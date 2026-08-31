# -*- coding: utf-8 -*-
"""P3.3：精确重合线去重（LINE + LWPOLYLINE 重复绘制同一根杆）回归测试。

背景（35A1-JC1-05 实测）：同一图元在 DXF 画两遍（一次 LINE、一次
LWPOLYLINE），端点坐标差 ~0.3-0.4 图纸单位 → 提取器各提一根 = 完全
重合双杆（8 对）。double_line_merge 任何 offset 参数在 05 图都会误伤
真实 X 交叉对（实测 TP@500 211→208/194），故用本规则只删精确重合线。
"""
import unittest

from traceability.intake.tower_dxf import _dedup_exact_overlap_segments
from traceability.intake.tower_spec import exact_overlap_dedup_tolerance


class ExactOverlapDedupTest(unittest.TestCase):
    def _seg(self, handle, start, end, layer="0"):
        return {"handle": handle, "start": start, "end": end, "layer": layer}

    def test_removes_copied_line_pair(self):
        """核心：LINE + LWPOLYLINE 复制对（端点差 < tol）只留一根。"""
        segs = [
            self._seg("51F", (34403.79, -9672.23), (34520.43, -9565.29)),
            self._seg("523", (34403.45, -9671.87), (34520.10, -9564.92)),
        ]
        out = _dedup_exact_overlap_segments(segs, 0.6)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["handle"], "51F", "保留先出现者")

    def test_reversed_endpoint_order_counts_as_copy(self):
        """端点反向的复制线（start↔end 对调）也算重合。"""
        segs = [
            self._seg("A", (100.0, 100.0), (200.0, 200.0)),
            self._seg("B", (200.05, 200.05), (99.95, 100.02)),
        ]
        out = _dedup_exact_overlap_segments(segs, 0.6)
        self.assertEqual(len(out), 1)

    def test_keeps_near_parallel_real_bars(self):
        """红线：近平行近距的真实构件（X 交叉对）不得被误删。"""
        segs = [
            # 两根 X 交叉杆：中点近（交叉点）但端点远
            self._seg("X1", (-1250.8, 4912.0), (1080.1, 7050.5)),
            self._seg("X2", (-1250.8, 7050.5), (1080.1, 4912.0)),
        ]
        out = _dedup_exact_overlap_segments(segs, 0.6)
        self.assertEqual(len(out), 2, "X 交叉对端点距 > 阈值，必须都保留")

    def test_boundary_tolerance_case(self):
        """实测坑：端点和 = 1.00007（容差 0.5 → 阈值 1.0）被浮点边界挡住。
        容差 0.6（阈值 1.2）须能覆盖。"""
        segs = [
            self._seg("51F", (34403.791446, -9672.233613),
                      (34520.433946, -9565.287113)),
            self._seg("523", (34403.453446, -9671.865113),
                      (34520.095946, -9564.918613)),
        ]
        # 容差 0.5：端点和 1.00007 > 1.0 → 不删（旧行为的坑）
        out05 = _dedup_exact_overlap_segments(segs, 0.5)
        # 容差 0.6：1.00007 < 1.2 → 删
        out06 = _dedup_exact_overlap_segments(segs, 0.6)
        self.assertEqual(len(out06), 1)

    def test_empty_input(self):
        self.assertEqual(_dedup_exact_overlap_segments([], 0.6), [])

    def test_config_reader(self):
        """overlay 配置读取：未配置 stem 返回 None，配置返回 float。"""
        self.assertIsNone(exact_overlap_dedup_tolerance("35A1-JC1-02"))
        tol = exact_overlap_dedup_tolerance(
            "35A1-JC1-05",
            overlay="examples/external/guowang_35A1/layer_overlay.json")
        self.assertIsNotNone(tol)
        self.assertGreater(tol, 0)


if __name__ == "__main__":
    unittest.main()
