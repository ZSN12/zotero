# -*- coding: utf-8 -*-
"""LevelGridSolver 单元测试（P2，设计见 docs/LEVEL_GRID_SOLVER_DESIGN.md）。

夹具纪律：合成场景覆盖实测病灶（稀疏桥并簇/桶均值拉偏/吸收半径边界），
真实产物验证走 scripts/validate_level_grid.py（离线，两塔门禁 85%）。
"""
import unittest
from pathlib import Path

from traceability.solve.level_grid import (
    ABSORB_MM, FILL_MIN_DIST_MM, ORIGIN_WEIGHTS,
    endpoints_from_sheet_model, grid_from_sheets_dir, vote_level_grid)


def _eps(specs):
    """[(z, bar_id, origin), ...] 快捷构造。"""
    return [(float(z), bid, org) for z, bid, org in specs]


class VoteLevelGridTest(unittest.TestCase):
    def test_basic_voting_and_markers(self):
        """常规场景：几何簇 + 标注层 → 网格含两者。"""
        eps = {"S1": _eps([
            (8000.0, "b1", "dxf_geom"), (8000.0, "b2", "dxf_geom"),
            (8002.0, "b3", "diag_synth"), (12000.0, "b4", "dxf_geom"),
            (12001.0, "b5", "dxf_geom"), (8300.0, "b6", "dxf_geom"),
            (8301.0, "b7", "diag_synth"),
        ])}
        levels, records = vote_level_grid(
            eps, {"S1": [8500.0, 11500.0]}, {"S1": 7000.0})
        self.assertIn(8500.0, levels)
        self.assertIn(11500.0, levels)
        self.assertIn(7000.0, levels)  # 边界锚
        # 8000 簇（3 杆）应在网格（离 8500 锚 500 ≥ FILL_MIN_DIST）
        self.assertTrue(any(abs(l - 8000.0) < 1 for l in levels))
        # 12000 簇（2 杆）同样
        self.assertTrue(any(abs(l - 12000.0) < 1 for l in levels))
        # 记录可追溯：marker 层带 kind
        kinds = {r["kind"] for r in records}
        self.assertEqual(kinds, {"marker", "boundary", "geom"})

    def test_weak_bucket_bridge_does_not_merge(self):
        """实测病灶（07 册）：单杆稀疏桶不得把 7000-11400 糊成一簇。"""
        eps = []
        # 强簇 8000（3 杆）与 9200（3 杆）之间铺单杆桥（8400/8600/8800）
        for bid, z in (("b1", 8000), ("b2", 8001), ("b3", 8002),
                       ("b4", 8400), ("b5", 8600), ("b6", 8800),
                       ("b7", 9200), ("b8", 9201), ("b9", 9202)):
            eps.append((float(z), bid, "dxf_geom"))
        levels, _ = vote_level_grid({"S1": eps}, {}, {"S1": 7000.0})
        self.assertTrue(any(abs(l - 8000) <= 100 for l in levels))
        self.assertTrue(any(abs(l - 9200) <= 100 for l in levels),
                        f"9200 簇被稀疏桥吞并: {levels}")

    def test_peeling_emits_two_peaks_in_one_chain(self):
        """实测病灶（07 册 [9900..11100]）：一条链剥出双层。"""
        eps = []
        # 9900 强峰 + 11100 弱峰，链距内（nuclei gap ≤400）
        for i, z in enumerate((9900, 9901, 9902, 10050, 10400, 10750, 11100, 11101)):
            eps.append((float(z), f"b{i}", "diag_synth" if i % 2 else "dxf_geom"))
        # 每桶 ≥2 独立杆才成核：再补一份
        for i, z in enumerate((9900, 9901, 11100, 11101)):
            eps.append((float(z), f"c{i}", "dxf_geom"))
        levels, _ = vote_level_grid({"S1": eps}, {}, {"S1": 9500.0})
        self.assertTrue(any(abs(l - 9900) <= 100 for l in levels))
        # 11100 弱核未被 9900 峰吸收（距离 1200 > ABSORB）
        self.assertTrue(any(abs(l - 11100) <= 100 for l in levels),
                        f"11100 层丢失: {levels}")

    def test_absorption_merges_close_sublevels(self):
        """14400/14500 相邻 100mm 子层允许并簇（吸收半径语义）。"""
        eps = []
        for i, z in enumerate((14400, 14410, 14500, 14510)):
            eps.append((float(z), f"b{i}", "dxf_geom"))
        levels, _ = vote_level_grid({"S1": eps}, {}, {"S1": 14000.0})
        near = [l for l in levels if 14300 <= l <= 14600]
        self.assertEqual(len(near), 1, f"100mm 子层应并为一层: {levels}")

    def test_leg_synth_never_votes(self):
        """纪律：leg_synth（表驱动，P2.6 注入通道）端点不入投票。"""
        eps = {"S1": _eps([
            (9999.0, "ls1", "leg_synth"), (9999.0, "ls2", "leg_synth"),
        ])}
        levels, _ = vote_level_grid(eps, {}, {"S1": 7000.0})
        self.assertEqual([l for l in levels if abs(l - 9999) <= 400], [])

    def test_marker_priority_over_boundary(self):
        """锚合并时 marker 值优先（ZC1 datum 实测非整值场景）。"""
        levels, records = vote_level_grid(
            {}, {"S1": [19400.0]}, {"S1": 19131.0})
        self.assertEqual(levels, [19400.0])
        self.assertEqual(records[0]["kind"], "marker")

    def test_unaligned_sheet_skipped(self):
        """无 datum 册（z 未复原）不投票——端点被忽略。"""
        levels, _ = vote_level_grid(
            {"S-noDatum": _eps([(5000, "b1", "dxf_geom"), (5000, "b2", "dxf_geom")])},
            {}, {"S1": 7000.0})
        self.assertEqual(levels, [7000.0])


class SheetLoaderTest(unittest.TestCase):
    def _sheet_model(self):
        return {
            "components": {
                "N1": {"kind": "tower_node", "properties": {
                    "view_y": 2000.0, "view_x": 10.0}},
                "N2": {"kind": "tower_node", "properties": {
                    "view_y": 3500.0, "view_x": 20.0}},
                "B1": {"kind": "tower_bar", "properties": {
                    "from_node": "N1", "to_node": "N2",
                    "geometry_origin": "dxf_geom"}},
                "B2": {"kind": "tower_bar", "properties": {
                    "from_node": "N1", "to_node": "N2",
                    "geometry_origin": "leg_synth"}},
            }
        }

    def test_endpoints_recover_z_and_filter_origin(self):
        eps = endpoints_from_sheet_model(self._sheet_model(), 7000.0)
        # 只有 dxf_geom 杆投票；z = view_y + datum
        self.assertEqual(sorted(eps), [(9000.0, "B1", "dxf_geom"),
                                       (10500.0, "B1", "dxf_geom")])

    def test_grid_from_sheets_dir_skips_unanchored(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sdir = root / "sheets"
            sdir.mkdir()
            (sdir / "S-datum.json").write_text(
                json.dumps(self._sheet_model()), encoding="utf-8")
            (sdir / "S-noDatum.json").write_text(
                json.dumps(self._sheet_model()), encoding="utf-8")
            overlay = {
                "view_regions": {"S-datum": [{"z_offset": 7000.0}],
                                 "S-noDatum": []},
                "centerline_extract": {"S-datum": {"beam_marker_levels_mm": [9500.0]}},
            }
            levels, records, warnings = grid_from_sheets_dir(sdir, overlay)
            self.assertIn(9500.0, levels)
            self.assertEqual([w for w in warnings if "S-noDatum" in w],
                             ["S-noDatum: 无 datum（view_regions），跳过投票"])


if __name__ == "__main__":
    unittest.main()


class BeatAnchorTest(unittest.TestCase):
    def test_beat_anchors_join_grid_as_anchor(self):
        """尺寸节拍（第三证据源）入锚骨架，w2 级、marker 优先级更高。"""
        levels, records = vote_level_grid(
            {}, {"S1": [8000.0]}, {"S1": 7000.0},
            beat_anchors={"S1": [7400.0, 7760.0]})
        self.assertEqual(levels, [7000.0, 7400.0, 7760.0, 8000.0])
        beats = [r for r in records if r["kind"] == "beat"]
        self.assertEqual(len(beats), 2)

    def test_beat_anchors_from_cross_file_filters_degenerate(self):
        """n_beats 退化（region_span_linear 端点两值）不投票。"""
        model = {"components": {"df": {"kind": "drawing_file", "properties": {
            "dimension_beat_anchors_by_sheet": {
                "S-real": {"z": [12000, 12400, 12800, 13200], "n_beats": 3},
                "S-degenerate": {"z": [17000, 24000], "n_beats": 0},
                "S-empty": {"z": []},
            }}}}}
        from traceability.solve.level_grid import beat_anchors_from_cross_file
        self.assertEqual(beat_anchors_from_cross_file(model), {"S-real": [12000.0, 12400.0, 12800.0, 13200.0]})
