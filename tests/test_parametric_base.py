# -*- coding: utf-8 -*-
"""P5：底段参数化外推回归测试（锥线延拓 + 通长腿 + 口径隔离）。"""
import unittest


def _fixture():
    """最低腿证据 z=6700（|x|=2300），上有腿链到 z=20000。"""
    nodes = {}
    bars = []
    k = 0
    for z, x in [(6700, 2300), (10000, 2100), (13000, 1900), (16000, 1700)]:
        for sx in (-1, 1):
            nodes[f"L{k}"] = (sx * x, 0.0, float(z))
            k += 1
    # 腿杆（近竖直）
    for z0, z1 in [(6700, 10000), (10000, 13000), (13000, 16000)]:
        x0 = 2300 - (z0 - 6700) * 0.0667
        x1 = 2300 - (z1 - 6700) * 0.0667
        for sx in (-1, 1):
            bars.append({"id": f"leg_{z0}_{sx}", "from": f"n_{z0}_{sx}",
                         "to": f"n_{z1}_{sx}"})
    # 重建节点引用（上面循环有误——直接造）
    nodes = {}
    bars = []
    k = 0
    for z, x in [(6700, 2300), (10000, 2100), (13000, 1900), (16000, 1700)]:
        for sx in (-1, 1):
            nodes[f"n_{z}_{'p' if sx > 0 else 'n'}"] = (sx * x, 0.0, float(z))
    for z0, z1 in [(6700, 10000), (10000, 13000), (13000, 16000)]:
        for sx, tag in ((-1, 'n'), (1, 'p')):
            bars.append({"id": f"leg_{z0}_{tag}",
                         "from": f"n_{z0}_{tag}", "to": f"n_{z1}_{tag}"})
    return nodes, bars


class LegChainExtrapolatorTest(unittest.TestCase):
    def test_extrapolates_below_evidence(self):
        """z < 腿证据下界：用延拓线（锥度，非夹紧常数）。"""
        from traceability.solve.tower_geometry import leg_chain_extrapolator
        nodes, bars = _fixture()
        fn = leg_chain_extrapolator(nodes, bars, base_fn=lambda z: 2300.0)
        self.assertIsNotNone(fn)
        hw0, hw6500 = fn(0.0), fn(6500.0)
        # 锥线：z=0 比 z=6500 宽（收窄斜率 < 0）
        self.assertGreater(hw0, hw6500)
        self.assertGreater(hw0, 2300.0, "z=0 须比证据下界宽（向下延拓）")

    def test_base_fn_used_above_evidence(self):
        """z >= 证据下界：回落 base_fn（上段行为零改变）。"""
        from traceability.solve.tower_geometry import leg_chain_extrapolator
        nodes, bars = _fixture()
        fn = leg_chain_extrapolator(nodes, bars, base_fn=lambda z: 2300.0)
        self.assertAlmostEqual(fn(6700.0), 2300.0, places=3)
        self.assertAlmostEqual(fn(20000.0), 2300.0, places=3)

    def test_no_leg_evidence_returns_none(self):
        """无近竖直腿：返回 None（调用方回退原 fit）。"""
        from traceability.solve.tower_geometry import leg_chain_extrapolator
        nodes = {"a": (100.0, 0.0, 1000.0), "b": (120.0, 0.0, 5000.0)}
        bars = [{"id": "diag", "from": "a", "to": "b"}]  # 斜杆非腿
        self.assertIsNone(leg_chain_extrapolator(nodes, bars))


class ExtrapolateBaseSegmentTest(unittest.TestCase):
    def test_through_leg_topology(self):
        """通长腿样式：每层起点 → z_top 通长杆（与 GT 底段同构）。"""
        from traceability.solve.tower_geometry import extrapolate_base_segment
        nodes, bars = _fixture()
        fn = lambda z: 2300.0 - 0.0667 * max(z - 6700, 0) if z < 6700 else 2300.0 - 0.0667 * (z - 6700)
        nn, nb, rep = extrapolate_base_segment(nodes, bars, lambda z: 2300.0 - 0.0667 * (z - 6700),
                                               z_top=6500.0)
        legs = [b for b in nb if b["role"] == "LEG"]
        # levels [0..6000, 6500]：0~6000 起点各 2 根（左右）通长到 6500
        self.assertEqual(len(legs), 14)
        self.assertEqual(rep["leg_topology"], "through_to_ztop")
        # 全部 derived_parametric 语义
        self.assertTrue(all(b["geometry_class"] == "derived_parametric" for b in nb))
        self.assertTrue(all(b["geometry_origin"] == "derived_parametric_base" for b in nb))

    def test_caliber_isolation(self):
        """口径隔离：外推杆只进 parametric/full，不进 pure。"""
        from traceability.eval.metrics import _bar_caliber_class, is_physical_bar
        p = {"geometry_class": "derived_parametric",
             "geometry_origin": "derived_parametric_base",
             "evidence_status": "reconstructed", "face": "f"}
        self.assertEqual(_bar_caliber_class(p), "parametric")
        self.assertTrue(is_physical_bar(p))


if __name__ == "__main__":
    unittest.main()
