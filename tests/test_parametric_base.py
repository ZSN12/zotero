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

    def test_leg_synth_pollution_regression(self):
        """P5.2（2026-09-05）：leg_synth 跨型腿不得污染延拓线。

        实测事故（35A1-JC1）：leg_synth 腿低 z 端点 |x| 继承夹紧 hw，
        与最低 dxf 腿点组成零斜率对 → 旧逻辑直接 return None →
        底段半宽退回夹紧常数 → 裙部全平底（GT 张开 2762@z0）。
        修复：合成来源杆被排除 + 零斜率点对跳过继续找。
        """
        from traceability.solve.tower_geometry import leg_chain_extrapolator
        nodes, bars = _fixture()  # dxf 腿证据：z>=6700
        # 模拟 leg_synth 跨型腿：z=6800 起点挂到夹紧半宽 2300（与
        # 最低 dxf 腿点 (6700, 2300) 同 |x| → 零斜率对）
        nodes["ls_lo"] = (2300.0, 0.0, 6800.0)
        nodes["ls_hi"] = (2100.0, 0.0, 10000.0)
        bars.append({"id": "ls1", "from": "ls_lo", "to": "ls_hi",
                     "geometry_origin": "leg_synth"})
        fn = leg_chain_extrapolator(nodes, bars, base_fn=lambda z: 2300.0)
        # 修复前：None（退回夹紧常数 → 平底裙部）
        self.assertIsNotNone(fn, "leg_synth 污染不得让延拓失败")
        self.assertGreater(fn(0.0), 2400.0,
                           "z=0 半宽须显著张开（锥线延拓生效）")

    def test_zero_slope_pair_skipped(self):
        """P5.2（续）：最低两点同 |x|（零斜率）时跳过该对，找下一个可用点。"""
        from traceability.solve.tower_geometry import leg_chain_extrapolator
        # 最低两个腿点同 |x|=2300（z 6700 与 6800），上有真实锥度腿
        nodes = {
            "a": (-2300.0, 0.0, 6700.0), "b": (2300.0, 0.0, 6700.0),
            "c": (-2300.0, 0.0, 6800.0), "d": (2300.0, 0.0, 6800.0),
            "e": (-2100.0, 0.0, 10000.0), "f": (2100.0, 0.0, 10000.0),
        }
        bars = [
            {"id": "l1", "from": "a", "to": "e"},
            {"id": "l2", "from": "b", "to": "f"},
        ]
        fn = leg_chain_extrapolator(nodes, bars, base_fn=lambda z: 2300.0)
        # 修复前：最低点对 (6700,2300)-(6800,2300) 零斜率 → None
        self.assertIsNotNone(fn)
        self.assertGreater(fn(0.0), fn(6700.0))


class ExtrapolateBaseSegmentTest(unittest.TestCase):
    def test_skirt_fan_spokes_topology(self):
        """S8 裙部 fan-spokes：spoke 层腿 + z_top 中点辐条（GT 底段同构）。"""
        from traceability.solve.tower_geometry import extrapolate_base_segment
        nodes, bars = _fixture()
        nn, nb, rep = extrapolate_base_segment(
            nodes, bars, lambda z: 2300.0 - 0.0667 * (z - 6700),
            z_top=6500.0)
        legs = [b for b in nb if b["role"] == "LEG"]
        spokes = [b for b in nb if b["parametric_struct"] == "parametric_spoke"]
        # skirt 2500 → spoke 层 [0,1000,2000,3000,4000]：
        # 腿 5×2=10 pattern 杆 + 辐条 5×2=10 pattern 杆
        self.assertEqual(len(legs), 10)
        self.assertEqual(len(spokes), 10)
        self.assertEqual(rep["leg_topology"], "skirt_fan_spokes")
        self.assertEqual(rep["spoke_levels"], [0.0, 1000.0, 2000.0, 3000.0, 4000.0])
        # 全部 derived_parametric 语义
        self.assertTrue(all(b["geometry_class"] == "derived_parametric" for b in nb))
        self.assertTrue(all(b["geometry_origin"] == "derived_parametric_base" for b in nb))
        # 辐条从 z_top 中点 (0,0,6500) 出发
        mid = [n for n, p in nn.items() if abs(p[0]) < 1e-9 and abs(p[1]) < 1e-9]
        self.assertEqual(len(mid), 1)
        for sp in spokes:
            self.assertEqual(sp["from"], mid[0])

    def test_no_spokes(self):
        """add_spokes=False：只生成腿杆。"""
        from traceability.solve.tower_geometry import extrapolate_base_segment
        nodes, bars = _fixture()
        nn, nb, rep = extrapolate_base_segment(
            nodes, bars, lambda z: 2300.0 - 0.0667 * (z - 6700),
            z_top=6500.0, add_spokes=False)
        self.assertTrue(all(b["parametric_struct"] == "parametric_leg" for b in nb))
        self.assertEqual(rep["spokes"], 0)

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
