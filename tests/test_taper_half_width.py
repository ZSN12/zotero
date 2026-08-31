# -*- coding: utf-8 -*-
"""S7 锥体重建（Theil-Sen 稳健回归）+ 生产横担层检测 单元测试。

覆盖（Phase 1 验收）：
    1. Theil-Sen 抗离群：横担箱（高侧离群）不拉偏塔身锥线；
    2. 采样修正：节间化短主腿（~1m/段）+ 幽灵长腿共存时仍取全竖直端点；
    3. 变坡回退：两段式塔身（非单一锥体）返回 None 走 monotone；
    4. 横担层检测：宽节点聚类成层、闭包门控正确、无横担返回 None；
    5. crossarm_preserve_t：生产模式保留桁架内部节点 t。
"""

import math
import unittest

from traceability.solve import tower_geometry as g


def _make_face(leg_line, z_lo, z_hi, step=1000.0, stub_len=800.0, crossarm=None):
    """构造单立面节点/杆件：主腿沿 leg_line(z) 折线，节间化短段 + 内部竖杆。

    leg_line: callable z -> 半宽 mm
    crossarm: Optional[List[(z, arm_mm)]] 横担层（端头 |t|=arm_mm）
    """
    nodes = {}
    bars = []
    zs = [z_lo + i * step for i in range(int((z_hi - z_lo) / step) + 1)]

    def _leg(z):
        return leg_line(z)

    for side, sgn in (("L", 1.0), ("R", -1.0)):
        prev = None
        for z in zs:
            nid = f"{side}{int(z)}"
            nodes[nid] = (sgn * _leg(z), 0.0, z)
            if prev is not None:
                bars.append({"id": f"{side}{int(z)}b", "from": prev, "to": nid})
            prev = nid

    # 内部竖杆（|x| 小，污染采样）
    for i in range(0, len(zs) - 1):
        z = zs[i]
        a, b = f"iv{i}a", f"iv{i}b"
        nodes[a] = (50.0, 0.0, z)
        nodes[b] = (60.0, 0.0, z + step)
        bars.append({"id": f"iv{i}", "from": a, "to": b})

    if crossarm:
        for j, (cz, arm) in enumerate(crossarm):
            a, b = f"ca{j}a", f"ca{j}b"
            nodes[a] = (-arm, 0.0, cz)
            nodes[b] = (arm, 0.0, cz)
            bars.append({"id": f"ca{j}", "from": a, "to": b})

    return nodes, bars


class TheilSenFitTest(unittest.TestCase):
    """Theil-Sen 稳健回归核心性质。"""

    def test_exact_line(self):
        zs = [0, 1000, 2000, 3000, 4000, 5000]
        hs = [2000 - 0.07 * z for z in zs]
        fit = g._theil_sen_fit(zs, hs)
        self.assertIsNotNone(fit)
        b0, k = fit
        self.assertAlmostEqual(b0, 2000.0, delta=1e-6)
        self.assertAlmostEqual(k, -0.07, delta=1e-6)

    def test_outlier_robustness(self):
        # 8 个在线上 + 2 个高侧离群（横担）：斜率中位数不受影响
        zs = [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000]
        hs = [2000 - 0.07 * z for z in zs]
        hs[3] += 1500.0
        hs[7] += 1200.0
        fit = g._theil_sen_fit(zs, hs)
        b0, k = fit
        self.assertAlmostEqual(k, -0.07, delta=0.002)
        self.assertAlmostEqual(b0, 2000.0, delta=50.0)

    def test_insufficient_points(self):
        self.assertIsNone(g._theil_sen_fit([1.0], [2.0]))
        self.assertIsNone(g._theil_sen_fit([], []))


class TaperProfileTest(unittest.TestCase):
    """_fit_taper_profile：锥线拟合 + 横担箱剔除 + 变坡回退。"""

    def _body_bins(self, n=16, z0=6500.0, step=250.0):
        z_pts, hw_pts = [], []
        for i in range(n):
            z = z0 + i * step
            z_pts.append(z)
            hw_pts.append(2649.0 - 0.0687 * z)
        return z_pts, hw_pts

    def test_single_taper(self):
        z_pts, hw_pts = self._body_bins()
        fn = g._fit_taper_profile(z_pts, hw_pts, inlier_tol_mm=150.0)
        self.assertIsNotNone(fn)
        for z in (7000.0, 12000.0, 16000.0):
            self.assertAlmostEqual(fn(z), 2649.0 - 0.0687 * z, delta=30.0)

    def test_crossarm_bins_excluded(self):
        # 塔头横担箱（高侧 +1500mm）紧接塔身段：迭代剔除后锥线不偏
        z_pts, hw_pts = self._body_bins(n=36)  # 6500~15250
        for cz in (15300.0, 15550.0, 16000.0, 16250.0):
            z_pts.append(cz)
            hw_pts.append((2649.0 - 0.0687 * cz) + 1500.0)
        fn = g._fit_taper_profile(z_pts, hw_pts, inlier_tol_mm=150.0)
        self.assertIsNotNone(fn, "横担箱应被剔除而非整体回退")
        self.assertAlmostEqual(fn(7000.0), 2649.0 - 0.0687 * 7000.0, delta=30.0)
        self.assertAlmostEqual(fn(12000.0), 2649.0 - 0.0687 * 12000.0, delta=30.0)

    def test_two_segment_tower_rejected(self):
        # 两段式变坡：下段斜率 -0.07，上段陡收 -0.2——非单一锥体应回退 None
        z_pts, hw_pts = [], []
        for i in range(12):
            z = 6500.0 + i * 500.0
            z_pts.append(z)
            hw_pts.append(2649.0 - 0.0687 * z)
        z1 = z_pts[-1]
        h1 = hw_pts[-1]
        for i in range(1, 10):
            z = z1 + i * 500.0
            z_pts.append(z)
            hw_pts.append(h1 - 0.2 * (z - z1))
        fn = g._fit_taper_profile(z_pts, hw_pts, inlier_tol_mm=150.0)
        self.assertIsNone(fn, "两段式变坡应拒绝拟合（内点比例不足）")

    def test_slope_positive_rejected(self):
        # 向上变宽（物理不可能）：k>0 拒绝
        z_pts = [6500.0 + i * 250.0 for i in range(10)]
        hw_pts = [1000.0 + 0.05 * z for z in z_pts]
        fn = g._fit_taper_profile(z_pts, hw_pts, inlier_tol_mm=150.0)
        self.assertIsNone(fn)


class TaperFromFaceTest(unittest.TestCase):
    """fit_tower_half_width_from_face(method='taper') 端到端。"""

    def test_taper_beats_plateau(self):
        # 12 段节间化主腿 + 内部竖杆 + 塔头横担 → taper 应贴真实锥线
        def leg_line(z):
            return 2649.0 - 0.0687 * z

        nodes, bars = _make_face(leg_line, 6500.0, 22000.0, step=1000.0,
                                 crossarm=[(21500.0, 1900.0)])
        fn = g.fit_tower_half_width_from_face(nodes, bars, method="taper")
        self.assertIsNotNone(fn)
        # 验收区间 z ∈ [7000, 16000] 残差 ≤ 30mm
        errs = [abs(fn(z) - leg_line(z)) for z in range(7000, 16500, 250)]
        self.assertLessEqual(sorted(errs)[len(errs) // 2], 30.0,
                             "验收：锥线残差中位 ≤30mm")
        # 塔身中部不得出现常数平台（相邻 2m 半宽差应 >100mm）
        for z in (8000.0, 10000.0, 12000.0):
            drop = fn(z) - fn(z + 2000.0)
            self.assertGreater(drop, 100.0,
                               f"z={z} 处出现平台（2m 半宽差 {drop:.0f}mm）")

    def test_monotone_fallback_on_two_segment(self):
        def leg_line(z):
            if z < 14000.0:
                return 2400.0 - 0.07 * z
            return 1420.0 - 0.15 * (z - 14000.0)

        nodes, bars = _make_face(leg_line, 6500.0, 22000.0, step=1000.0)
        fn = g.fit_tower_half_width_from_face(nodes, bars, method="taper")
        # 变坡 → taper 拒绝 → 回退 monotone（非 None，分段近似）
        self.assertIsNotNone(fn)

    def test_short_subdivided_legs_sampled(self):
        # 节间化短腿（step=1000 < min_leg_len 2500）必须仍能采样
        def leg_line(z):
            return 2000.0 - 0.06 * z

        nodes, bars = _make_face(leg_line, 6500.0, 16500.0, step=1000.0)
        fn = g.fit_tower_half_width_from_face(nodes, bars, method="taper")
        self.assertIsNotNone(fn)
        self.assertAlmostEqual(fn(8000.0), 2000.0 - 0.06 * 8000.0, delta=60.0)


class CrossarmDetectTest(unittest.TestCase):
    """生产横担层检测。"""

    def _body_line(self, z):
        return max(1.0, 2649.0 - 0.0687 * z)

    def test_layers_detected_and_gated(self):
        def leg_line(z):
            return 2649.0 - 0.0687 * z

        nodes, bars = _make_face(
            leg_line, 6500.0, 22000.0, step=1000.0,
            crossarm=[(20000.0, 2200.0), (20500.0, 1900.0), (21000.0, 1134.0)])
        arm_fn, rep = g.detect_crossarm_layers_from_face(nodes, bars, self._body_line)
        self.assertIsNotNone(arm_fn)
        layers = rep["layers"]
        self.assertGreaterEqual(len(layers), 1)
        arm_max = max(l["arm_mm"] for l in layers)
        self.assertAlmostEqual(arm_max, 2200.0, delta=100.0)
        # 横担层 z 处门开
        self.assertGreater(arm_fn(20000.0), 0.0)
        self.assertGreater(arm_fn(20500.0), 0.0)
        self.assertGreater(arm_fn(21000.0), 0.0)
        # 塔身段（无横担）门关
        self.assertEqual(arm_fn(8000.0), 0.0)
        self.assertEqual(arm_fn(15000.0), 0.0)
        self.assertEqual(arm_fn(6500.0), 0.0)

    def test_no_crossarm_returns_none(self):
        def leg_line(z):
            return 2000.0 - 0.07 * z

        nodes, bars = _make_face(leg_line, 6500.0, 22000.0, step=1000.0)
        arm_fn, rep = g.detect_crossarm_layers_from_face(nodes, bars, self._body_line)
        self.assertIsNone(arm_fn)
        self.assertEqual(rep["layers"], [])

    def test_layer_span_padding(self):
        def leg_line(z):
            return 2649.0 - 0.0687 * z

        nodes, bars = _make_face(leg_line, 6500.0, 22000.0, step=1000.0,
                                 crossarm=[(20000.0, 2200.0)])
        arm_fn, _ = g.detect_crossarm_layers_from_face(nodes, bars, self._body_line)
        # 层范围 ±750mm 填充：桁架上下节点也在层内
        self.assertGreater(arm_fn(20000.0 - 700.0), 0.0)
        self.assertEqual(arm_fn(20000.0 - 900.0), 0.0)


class CrossarmPreserveTTest(unittest.TestCase):
    """expand_4_face_symmetry 的 crossarm_preserve_t 生产行为。"""

    def _face(self):
        def leg_line(z):
            return max(600.0, 2649.0 - 0.0687 * z)

        nodes, bars = _make_face(
            leg_line, 6500.0, 36000.0, step=1000.0,
            crossarm=[(30000.0, 2200.0)])
        # 横担桁架中间节点（|t| 在 1.3*w_gt 与 0.9*w_arm 之间——旧逻辑会推到外缘）
        for i, t in enumerate((900.0, 1400.0, 1800.0)):
            nodes[f"mid{i}"] = (t, 0.0, 30000.0)
        nodes["midbr"] = (-1400.0, 0.0, 30000.0)
        bars.append({"id": "mid0", "from": "mid0", "to": "mid1"})
        return nodes, bars

    def _body_fn(self, z):
        return max(1.0, 2649.0 - 0.0687 * z)

    def _arm_fn(self, z):
        return 2200.0 if abs(z - 30000.0) <= 750.0 else 0.0

    def test_preserve_t_keeps_mid_arm_nodes(self):
        nodes, bars = self._face()
        fn_nodes, fn_bars = g.expand_4_face_symmetry(
            nodes, bars, half_width_fn=self._body_fn,
            crossarm_half_width_fn=self._arm_fn,
            crossarm_preserve_t=True, add_diaphragms=False)
        xs = {round(p[0], 1) for p in fn_nodes.values()
              if abs(p[2] - 30000.0) < 1.0}
        # 中间桁架节点 |x|=1400 必须保留（不得被推到 2200）
        self.assertIn(1400.0, xs)
        self.assertNotIn(2200.0 * 0.636, xs)  # 防御：任何缩放痕迹

    def test_legacy_mode_pushes_to_tip(self):
        nodes, bars = self._face()
        fn_nodes, fn_bars = g.expand_4_face_symmetry(
            nodes, bars, half_width_fn=self._body_fn,
            crossarm_half_width_fn=self._arm_fn,
            crossarm_preserve_t=False, add_diaphragms=False)
        xs = {round(p[0], 1) for p in fn_nodes.values()
              if abs(p[2] - 30000.0) < 1.0}
        # 旧行为：|t|<0.9*w_arm 推到 ±2200
        self.assertIn(2200.0, xs)
        self.assertNotIn(1400.0, xs)


if __name__ == "__main__":
    unittest.main()
