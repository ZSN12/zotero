"""P1（06 段斜材拓扑闭环）单元测试。

覆盖：
  * 候选收集（角度过滤 / sheet 过滤 / face 过滤 / z 窗口）
  * 端点 z 聚类
  * FULL/HALF/MID 证据线分类
  * fan/twist 解释评分（端点 snap、跨度约束）
  * 主入口：生成杆语义（origin/level_source/source_handles）+ 原杆撤除
"""

import math
import unittest

from traceability.solve.diagonal_topology import (
    select_interpretations,
    build_interpretations,
    cluster_endpoint_heights,
    collect_diagonal_candidates,
    reconstruct_diagonal_topology,
    _classify_drawn_line,
)


def hw(z: float) -> float:
    """测试锥线：z=12000 → 1950，z=17000 → 1650（线性）。"""
    return 1950.0 + (z - 12000.0) * (1650.0 - 1950.0) / 5000.0


def make_model():
    """迷你模型：一根 FULL twist 线 + 一根 HALF fan 线（front 面）。"""
    nodes = {
        # FULL 线：角→对角（z 16486→14278，投影全宽）
        "n1": (hw(16486), 1600.0, 16486.0),
        "n2": (-hw(14278), 1600.0, 14278.0),
        # HALF 线：中心→角（z 15455→14391）
        "n3": (20.0, 1600.0, 15455.0),
        "n4": (hw(14391), 1600.0, 14391.0),
    }
    bars = [
        {"id": "35A1-JC1-06__bar_A_front_F", "from": "n1", "to": "n2",
         "face": "f", "role": "DIAG",
         "source_file": "35A1-JC1-06", "geometry_origin": "dxf_geom",
         "geometry_class": "recognized", "bar_id": "A", "layer": "1"},
        {"id": "35A1-JC1-06__bar_B_front_F", "from": "n3", "to": "n4",
         "face": "f", "role": "DIAG",
         "source_file": "35A1-JC1-06", "geometry_origin": "dxf_geom",
         "geometry_class": "recognized", "bar_id": "B", "layer": "1"},
        # 其它 sheet / 非斜材：不应入选
        {"id": "35A1-JC1-05__bar_C_front_F", "from": "n1", "to": "n2",
         "face": "f", "role": "DIAG",
         "source_file": "35A1-JC1-05", "geometry_origin": "dxf_geom",
         "geometry_class": "recognized", "bar_id": "C", "layer": "1"},
    ]
    return nodes, bars


class TestCollect(unittest.TestCase):
    def test_filters(self):
        nodes, bars = make_model()
        cands = collect_diagonal_candidates(
            nodes, bars, sheets=["35A1-JC1-06"],
            z_window=(11000.0, 17500.0))
        ids = {c["bar_id"] for c in cands}
        self.assertEqual(ids, {
            "35A1-JC1-06__bar_A_front_F", "35A1-JC1-06__bar_B_front_F"})
        # 候选记录字段（P1 2.1）
        c = cands[0]
        for k in ("bar_id", "source_handles", "source_region",
                  "endpoints", "length_2d", "inclination_deg"):
            self.assertIn(k, c)

    def test_z_window_excludes(self):
        nodes, bars = make_model()
        cands = collect_diagonal_candidates(
            nodes, bars, sheets=["35A1-JC1-06"], z_window=(1000.0, 2000.0))
        self.assertEqual(cands, [])


class TestHeights(unittest.TestCase):
    def test_cluster(self):
        nodes, bars = make_model()
        cands = collect_diagonal_candidates(
            nodes, bars, sheets=["35A1-JC1-06"])
        heights = cluster_endpoint_heights(cands, tol_mm=300.0)
        # 14391 与 14278 相距 113mm → 合并成一簇；其余各自成簇
        self.assertEqual(len(heights), 3)
        self.assertTrue(all(h["count"] >= 1 for h in heights))


class TestClassify(unittest.TestCase):
    def test_full_half_mid(self):
        self.assertEqual(
            _classify_drawn_line([(hw(16486), 16486), (-hw(14278), 14278)], hw),
            "FULL")
        self.assertEqual(
            _classify_drawn_line([(20.0, 15455), (hw(14391), 14391)], hw),
            "HALF")
        self.assertEqual(
            _classify_drawn_line([(0.45 * hw(15964), 15964),
                                  (hw(15370), 15370)], hw),
            "MID")
        # 短水平/竖直线不分类
        self.assertIsNone(_classify_drawn_line([(0.5, 100), (0.5, 200)], hw))


class TestInterpretations(unittest.TestCase):
    def test_fan_twist_pairs(self):
        nodes, bars = make_model()
        cands = collect_diagonal_candidates(
            nodes, bars, sheets=["35A1-JC1-06"])
        heights = cluster_endpoint_heights(cands)
        interps, sel_audit = build_interpretations(
            cands, heights, [14000.0, 16000.0], hw)
        kinds = sorted(r["kind"] for r in interps)
        # FULL 线 → twist；HALF 线 → fan（snap 到平台 16000 附近）
        self.assertIn("twist", kinds)
        self.assertIn("fan", kinds)
        for r in interps:
            self.assertLess(r["score"], 4000.0)
            self.assertTrue(r["evidence"])
        # P1.1：择优审计必然存在且记录 kept 数
        self.assertIn("kept", sel_audit)
        self.assertGreaterEqual(sel_audit["kept"], len(interps))


class TestMainEntry(unittest.TestCase):
    def _run(self, bars=None):
        nodes, base_bars = make_model()
        return reconstruct_diagonal_topology(
            nodes, bars if bars is not None else base_bars, hw,
            sheets=["35A1-JC1-06"],
            panel_levels=[14000.0, 16000.0],
            z_window=(11000.0, 17500.0),
            level_source_label="gt_canonical",
        )

    def test_reconstruct(self):
        new_nodes, new_bars, rep = self._run()
        # 生成杆存在且语义正确
        gen = [b for b in new_bars if b.get("diagonal_topology")]
        self.assertGreater(len(gen), 0)
        for b in gen:
            self.assertEqual(b["geometry_origin"],
                             "diagonal_topology_reconstructed")
            self.assertEqual(b["geometry_class"], "reconstructed")
            self.assertEqual(b["level_source"], "gt_canonical")
            self.assertTrue(b["source_handles"])
        # 原始证据杆四面拷贝撤除
        ids = {str(b.get("id")) for b in new_bars}
        for suffix in ("_F", "_B", "_L", "_R"):
            self.assertNotIn(f"35A1-JC1-06__bar_A_front{suffix}", ids)
        # 05 sheet 杆不受影响
        self.assertIn("35A1-JC1-05__bar_C_front_F", ids)
        # report 审计字段
        for k in ("n_candidates", "heights", "interpretations",
                  "generated", "removed_originals", "candidates"):
            self.assertIn(k, rep)

    def test_split_siblings_removed(self):
        """Degree=1 回归（2026-08-31）：同根 __splitNN / _NN 变体段必须整族
        撤除，否则残余短段两端悬空（06 段实测 5 处悬空断裂）。"""
        nodes, base_bars = make_model()
        bars = list(base_bars) + [
            {"id": "35A1-JC1-06__bar_A_front__split7_F", "from": "n1",
             "to": "n2", "face": "f", "role": "DIAG",
             "source_file": "35A1-JC1-06", "geometry_origin": "dxf_geom",
             "geometry_class": "recognized", "bar_id": "A", "layer": "1"},
            {"id": "35A1-JC1-06__bar_A_front_67_B", "from": "n1",
             "to": "n2", "face": "b", "role": "DIAG",
             "source_file": "35A1-JC1-06", "geometry_origin": "dxf_geom",
             "geometry_class": "recognized", "bar_id": "A", "layer": "1"},
            {"id": "35A1-JC1-06__bar_A_front_67__split9_L", "from": "n1",
             "to": "n2", "face": "l", "role": "DIAG",
             "source_file": "35A1-JC1-06", "geometry_origin": "dxf_geom",
             "geometry_class": "recognized", "bar_id": "A", "layer": "1"},
        ]
        _, new_bars, _ = reconstruct_diagonal_topology(
            nodes, bars, hw, sheets=["35A1-JC1-06"],
            panel_levels=[14000.0, 16000.0])
        ids = {str(b.get("id")) for b in new_bars}
        self.assertNotIn("35A1-JC1-06__bar_A_front__split7_F", ids)
        self.assertNotIn("35A1-JC1-06__bar_A_front_67_B", ids)
        self.assertNotIn("35A1-JC1-06__bar_A_front_67__split9_L", ids)
        # 整族撤除不误伤其它 sheet 的变体杆
        self.assertIn("35A1-JC1-05__bar_C_front_F", ids)

    def test_keep_originals(self):
        nodes, bars = make_model()
        _, new_bars, _ = reconstruct_diagonal_topology(
            nodes, bars, hw, sheets=["35A1-JC1-06"],
            panel_levels=[14000.0], keep_originals=True)
        ids = {str(b.get("id")) for b in new_bars}
        self.assertIn("35A1-JC1-06__bar_A_front_F", ids)


class TestSelectionP11(unittest.TestCase):
    """P1.1：fan 候选冲突图择优——节拍筛选/同 h 冗余/交叉保险。"""

    def _fan(self, h, P, score=100.0):
        return {"kind": "fan", "z_lo": h, "z_hi": P, "score": score,
                "evidence": ["e"], "n": 1}

    def test_span_off_grid_rejected(self):
        # JC1 节拍 d=1000：span 应落 {2000,3000,4000}±450。
        good = [self._fan(12000, 14000), self._fan(13000, 16000),
                self._fan(12000, 16000)]
        bad = [self._fan(14349, 19000),   # span 4651 → beat_err 651 拒
               self._fan(16488, 19000)]   # span 2511 → beat_err 489 拒
        kept, audit = select_interpretations(
            good + bad, [11000, 12000, 13000, 14000, 16000, 17000, 19000])
        kept_pairs = {(round(r["z_lo"], 1), round(r["z_hi"], 1)) for r in kept}
        for r in good:
            self.assertIn((round(r["z_lo"], 1), round(r["z_hi"], 1)), kept_pairs)
        for r in bad:
            self.assertNotIn((round(r["z_lo"], 1), round(r["z_hi"], 1)), kept_pairs)
        reasons = {x["reason"] for x in audit["rejected"]}
        self.assertEqual(reasons, {"span_off_grid"})
        self.assertEqual(audit["beat_unit"], 1000.0)

    def test_duplicate_h_capped(self):
        # 同一 h 三个 fan（真结构最多 2 个目标平台）
        interps = [self._fan(12000, 14000, 900),
                   self._fan(12000, 16000, 1500),
                   self._fan(12000, 19000, 3000)]  # span 7000 也 off-grid
        kept, audit = select_interpretations(
            interps, [11000, 12000, 13000, 14000, 16000, 17000, 19000])
        self.assertEqual(len([r for r in kept if r["kind"] == "fan"]), 2)

    def test_panel_crossing_rejected(self):
        # h 更大却扇向更低平台 → 区域交叉；按 h 升序先到者保留
        interps = [self._fan(12000, 16000, 100), self._fan(13000, 14000, 200)]
        kept, audit = select_interpretations(
            interps, [11000, 12000, 13000, 14000, 16000, 17000, 19000])
        pairs = {(round(r["z_lo"]), round(r["z_hi"])) for r in kept if r["kind"] == "fan"}
        self.assertIn((12000.0, 16000.0), pairs)
        self.assertNotIn((13000.0, 14000.0), pairs)
        reasons = [x["reason"] for x in audit["rejected"]]
        self.assertIn("panel_crossing", reasons)

    def test_no_panel_levels_passthrough(self):
        interps = [self._fan(12000, 19000)]
        kept, audit = select_interpretations(interps, [])
        self.assertEqual(len(kept), 1)
        self.assertIn("note", audit)

    def test_twist_untouched_by_beat(self):
        twist = {"kind": "twist", "z_lo": 14500.0, "z_hi": 17000.0,
                 "score": 100.0, "evidence": ["e"], "n": 1}
        kept, audit = select_interpretations(
            [twist], [11000, 12000, 13000, 14000, 16000, 17000, 19000])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["kind"], "twist")


if __name__ == "__main__":
    unittest.main()
