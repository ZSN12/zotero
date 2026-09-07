# -*- coding: utf-8 -*-
"""S8.4 headx 塔头面板链「诚实锚层」回归测试（2026-09-07 自举污染修复）。

背景（ZC1 实测）：complete_head_panel_chain 的角点轨迹簇曾统计全部
节点——模板自生成节点自成轨迹簇 → 伪锚层（30950/31967/34950/36222，
无 GT 层对应）→ 每伪层 10 杆水平环全 FP（8 层 80+ 杆 0 TP）。
修复：轨迹簇只采「被至少一根非 panel_template_completion 杆引用」
的证据节点。修复后锚层链与 GT 真实层位（30900/31600/32300/34900…）
一致。
"""
import unittest

from traceability.solve.tower_geometry import complete_head_panel_chain


def _hw_const(z: float) -> float:
    return 700.0


class HonestAnchorTest(unittest.TestCase):
    """角点轨迹簇的证据门控。"""

    def test_template_only_corner_nodes_are_not_anchors(self):
        """仅被模板杆引用的角点簇不得成为锚层（自举污染回归锁）。"""
        # 证据角点（被 dxf_geom 杆引用）：z=30000 一层
        nodes = {
            "ev0": (700.0, 700.0, 30000.0),
            "ev1": (-700.0, -700.0, 30000.0),
            "ev2": (700.0, -700.0, 30000.0),
            "ev3": (-700.0, 700.0, 30000.0),
            # 伪层角点：同构位置，但只被 panel_template_completion 引用
            "tpl0": (700.0, 700.0, 31500.0),
            "tpl1": (-700.0, -700.0, 31500.0),
            "tpl2": (700.0, -700.0, 31500.0),
            "tpl3": (-700.0, 700.0, 31500.0),
        }
        bars = [
            # 证据杆（非模板）：把 4 个 30000 角点连成水平环
            {"id": "e0", "from": "ev0", "to": "ev1", "geometry_origin": "dxf_geom"},
            {"id": "e1", "from": "ev2", "to": "ev3", "geometry_origin": "dxf_geom"},
            # 模板杆：引用伪层角点
            {"id": "t0", "from": "tpl0", "to": "tpl1",
             "geometry_origin": "panel_template_completion"},
            {"id": "t1", "from": "tpl2", "to": "tpl3",
             "geometry_origin": "panel_template_completion"},
        ]
        _n, _b, rep = complete_head_panel_chain(
            nodes, bars, _hw_const, [30000.0],
            z_min_mm=29500.0, level_source_label="test")
        levels = [float(z) for z in rep.get("levels", [])]
        self.assertIn(30000.0, levels)
        self.assertNotIn(31500.0, levels,
                         "模板节点轨迹簇不得成为锚层（自举污染）")

    def test_evidence_corner_nodes_still_anchor(self):
        """被非模板杆引用的角点簇仍应成为锚层（修复不误伤）。"""
        nodes = {
            "ev0": (700.0, 700.0, 30000.0),
            "ev1": (-700.0, -700.0, 30000.0),
            "ev2": (700.0, -700.0, 30000.0),
            "ev3": (-700.0, 700.0, 30000.0),
            "ev4": (700.0, 700.0, 33000.0),
            "ev5": (-700.0, -700.0, 33000.0),
            "ev6": (700.0, -700.0, 33000.0),
            "ev7": (-700.0, 700.0, 33000.0),
        }
        bars = [
            {"id": "e0", "from": "ev0", "to": "ev1", "geometry_origin": "dxf_geom"},
            {"id": "e1", "from": "ev2", "to": "ev3", "geometry_origin": "dxf_geom"},
            {"id": "e2", "from": "ev4", "to": "ev5", "geometry_origin": "dxf_geom"},
            {"id": "e3", "from": "ev6", "to": "ev7", "geometry_origin": "dxf_geom"},
        ]
        # anchor_levels 需非空（函数早退守卫）：给一个低横隔锚
        _n, _b, rep = complete_head_panel_chain(
            nodes, bars, _hw_const, [29000.0],
            z_min_mm=29500.0, level_source_label="test")
        levels = [float(z) for z in rep.get("levels", [])]
        self.assertIn(30000.0, levels)
        self.assertIn(33000.0, levels)

    def test_diaphragm_anchors_unaffected_by_gate(self):
        """横隔层锚（anchor_levels 传入）不受证据门影响。"""
        nodes = {"lonely": (700.0, 700.0, 30000.0)}
        bars = [{"id": "b", "from": "lonely", "to": "lonely",
                 "geometry_origin": "dxf_geom"}]
        _n, _b, rep = complete_head_panel_chain(
            nodes, bars, _hw_const, [30000.0],
            z_min_mm=29500.0, level_source_label="test")
        self.assertIn(30000.0, [float(z) for z in rep.get("levels", [])])


if __name__ == "__main__":
    unittest.main()
