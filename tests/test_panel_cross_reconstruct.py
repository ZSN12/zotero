# -*- coding: utf-8 -*-
"""Phase 3（P3.2/P3.3）：评分制节间 X 交叉重建回归测试。

锁定三层评分判据与口径隔离语义：
    1. 塔身区限定（z_hi < 横担层 z_lo 才生成）；
    2. 图纸斜线证据（节间±500mm 内 >= min_diag_evidence 根 dxf_geom 斜杆）；
    3. 腿位锚定（两端层各有 |x| >= min_leg_x_mm 节点，交叉对连接
       (x_lo_max, z_lo)→(-x_hi_max, z_hi) 与镜像）。
语义：geometry_origin=panel_cross_reconstructed（B 类，不入 pure 口径）。
"""
import math
import unittest

from traceability.solve.tower_geometry import reconstruct_panel_cross_diagonals


def _mk(nodes_spec, bars_spec):
    """nodes_spec: [(x, y, z)] → NodeMap；bars_spec: [(i, j, origin)] → bars。"""
    nodes = {f"n{k}": tuple(p) for k, p in enumerate(nodes_spec)}
    bars = []
    for k, (i, j, origin) in enumerate(bars_spec):
        bars.append({"id": f"b{k}", "from": f"n{i}", "to": f"n{j}",
                     "geometry_origin": origin})
    return nodes, bars


class PanelCrossReconstructTest(unittest.TestCase):
    """评分制 X 交叉重建的三层过滤 + 语义标记。"""

    def _base_fixture(self):
        """四层腿塔身（z=8000/10000/12000/14000），每层左右腿位 ±2000。"""
        nodes_spec = []
        # 每层左右腿节点（复用 = 低 degree-1）
        for z in (8000, 10000, 12000, 14000):
            nodes_spec.append((2000.0, 0.0, float(z)))
            nodes_spec.append((-2000.0, 0.0, float(z)))
        # 图纸斜线证据：节间 [8000,10000] 与 [12000,14000] 各 2 根
        # （倾角 >= 20°，长 >= 600mm），[10000,12000] 无证据。
        # 直接构造斜证据：补中间高度节点（z=9500/13000 → 倾角 ~28°/40°）
        nodes_spec.append((800.0, 0.0, 9500.0))    # n8：下节间中点
        nodes_spec.append((-800.0, 0.0, 9500.0))   # n9
        nodes_spec.append((800.0, 0.0, 13000.0))   # n10：上节间中点
        nodes_spec.append((-800.0, 0.0, 13000.0))  # n11
        bars_spec = [
            # 竖直腿（几何存在但倾角 >70° 不计斜证据）
            (0, 2, "dxf_geom"), (1, 3, "dxf_geom"),
            (2, 4, "dxf_geom"), (3, 5, "dxf_geom"),
            (4, 6, "dxf_geom"), (5, 7, "dxf_geom"),
            # 斜证据 2 根：[8000,10000] 节间（倾角 28.2°）
            (0, 9, "dxf_geom"),   # (2000,8000)→(-800,9500) 斜
            (1, 8, "dxf_geom"),   # (-2000,8000)→(800,9500) 斜
            # [12000,14000] 节间证据 2 根（倾角 39.8°）
            (6, 10, "dxf_geom"),  # (2000,14000)→(800,13000) 斜
            (7, 11, "dxf_geom"),  # (-2000,14000)→(-800,13000) 斜
        ]
        return _mk(nodes_spec, bars_spec)

    def test_scoring_generates_only_evidenced_panels(self):
        """核心：无证据节间不生成；有证据节间生成 2 根交叉对。"""
        nodes, bars = self._base_fixture()
        levels = [8000.0, 10000.0, 12000.0, 14000.0]
        nn, nb, rep = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=None,
        )
        # [8000,10000] 与 [12000,14000] 有证据 → 各 2 根；[10000,12000] 无 → 0
        self.assertEqual(rep["generated"], 4)
        self.assertEqual(len(rep["panels"]), 2)
        pzs = sorted((p["z_lo"], p["z_hi"]) for p in rep["panels"])
        self.assertIn((8000.0, 10000.0), pzs)
        self.assertIn((12000.0, 14000.0), pzs)
        self.assertNotIn((10000.0, 12000.0), pzs)
        # 生成杆的语义标记
        gen = [b for b in nb if b.get("panel_cross")]
        self.assertEqual(len(gen), 4)
        for b in gen:
            self.assertEqual(b["geometry_origin"], "panel_cross_reconstructed")
            self.assertEqual(b["geometry_class"], "reconstructed")

    def test_crossarm_zone_excluded(self):
        """塔身区限定：z_hi >= crossarm_z_max 的节间不生成（横担区斜线
        是桁架撑，不是塔身大交叉——实测误生成 d>800mm 的 FP）。"""
        nodes, bars = self._base_fixture()
        levels = [8000.0, 10000.0, 12000.0, 14000.0]
        # 横担层下界压到 12000：只有 [8000,10000] 一个节间可生成
        _, nb, rep = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=12000.0,
        )
        self.assertEqual(rep["generated"], 2)
        self.assertEqual(len(rep["panels"]), 1)
        self.assertEqual(rep["panels"][0]["z_hi"], 10000.0)

    def test_cross_pair_endpoints_mirrored(self):
        """交叉对连接：x_lo_max → -x_hi_max 与镜像（腿到对侧腿）。"""
        nodes, bars = self._base_fixture()
        levels = [8000.0, 10000.0]
        nn, nb, rep = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=None,
        )
        gen = [b for b in nb if b.get("panel_cross")]
        self.assertEqual(len(gen), 2)
        # 交叉形态：一根 (2000,8000)→(-2000,10000)，一根镜像
        ends = set()
        for b in gen:
            f, t = nn[b["from"]], nn[b["to"]]
            ends.add((round(f[0]), round(f[2]), round(t[0]), round(t[2])))
            ends.add((round(t[0]), round(t[2]), round(f[0]), round(f[2])))
        self.assertIn((2000, 8000, -2000, 10000), ends)
        self.assertIn((-2000, 8000, 2000, 10000), ends)

    def test_reuses_leg_nodes_no_new_dangling(self):
        """腿位锚定：端点复用现有腿节点（±300mm 内），不产生新孤立节点。"""
        nodes, bars = self._base_fixture()
        n_before = set(nodes.keys())
        levels = [8000.0, 10000.0]
        nn, nb, _ = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=None,
        )
        gen = [b for b in nb if b.get("panel_cross")]
        for b in gen:
            for nid in (b["from"], b["to"]):
                self.assertIn(nid, n_before,
                              "交叉端点必须复用现有腿节点（不新建孤立节点）")

    def test_min_diag_evidence_threshold(self):
        """证据阈值：证据数 < min_diag_evidence 的节间不生成。"""
        nodes, bars = self._base_fixture()
        levels = [8000.0, 10000.0]
        # 提高阈值到 5（只有 2 根证据）→ 不生成
        _, nb, rep = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=None, min_diag_evidence=5,
        )
        self.assertEqual(rep["generated"], 0)

    def test_level_gap_bounds(self):
        """层间距约束：gap < 1500 或 > 4500 的节间跳过。"""
        nodes, bars = self._base_fixture()
        # gap 1000（太矮）与 5000（太高）都不生成
        levels = [8000.0, 9000.0, 14000.0]
        _, _, rep = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=None,
        )
        # [8000,9000] gap=1000 跳过；[9000,14000] gap=5000 跳过
        # 但 [8000,14000]? levels 相邻对里没有这个组合
        self.assertEqual(rep["generated"], 0)

    def test_empty_levels_noop(self):
        """无层位：原样返回，不改动。"""
        nodes, bars = self._base_fixture()
        nn, nb, rep = reconstruct_panel_cross_diagonals(
            nodes, bars, [], crossarm_z_max=None,
        )
        self.assertEqual(rep["generated"], 0)
        self.assertEqual(len(nb), len(bars))
        self.assertEqual(len(nn), len(nodes))

    def test_level_source_label_passthrough(self):
        """level_source 跟随层位来源（gt_canonical→level_assisted 口径；
        dxf_derived→reconstructed 口径）——口径隔离的关键标记。"""
        nodes, bars = self._base_fixture()
        levels = [8000.0, 10000.0]
        _, nb, _ = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=None,
            level_source_label="dxf_derived",
        )
        gen = [b for b in nb if b.get("panel_cross")]
        self.assertTrue(all(b.get("level_source") == "dxf_derived" for b in gen))

    def test_only_dxf_geom_counts_as_evidence(self):
        """证据判据：只有 geometry_origin=dxf_geom 的斜杆算证据——
        重建杆自身（panel_cross_reconstructed）不得作为下一轮证据
        （防止级联自我繁殖）。"""
        nodes, bars = self._base_fixture()
        levels = [8000.0, 10000.0]
        nn, nb, _ = reconstruct_panel_cross_diagonals(
            nodes, bars, levels, crossarm_z_max=None,
        )
        # 把生成杆当输入再跑一遍：不应级联翻倍
        nn2, nb2, rep2 = reconstruct_panel_cross_diagonals(
            nn, nb, levels, crossarm_z_max=None,
        )
        self.assertEqual(rep2["generated"], 2,
                         "重建杆不算证据，第二轮不应级联翻倍")


if __name__ == "__main__":
    unittest.main()
