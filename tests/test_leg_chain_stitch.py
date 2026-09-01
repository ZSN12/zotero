# -*- coding: utf-8 -*-
"""P3.2 腿杆节间链合并（stitch_leg_chains）单元测试。

场景覆盖（真实病灶来自 2026-09-02 诊断：07 册腿被 beat 切成
830/213/998mm 碎片，GT 角柱是环层间 ~3.5m 整段）：
  1. 同节间共线碎片 → 合成 1 根整段（复用链端现存节点）；
  2. 跨平台层（panel 边界）必断链；
  3. 中间节点有外部杆挂接（度数>2）→ 断链保护；
  4. 重复段（同角同段两视图各画一次）去重；
  5. bar_id 剥离 + source_bar_ids 证据链；
  6. 不共线（夹角大）/gap 超限 → 不合并；
  7. 无 panel_levels → 原样返回。
"""
import unittest

from traceability.solve.tower_geometry import stitch_leg_chains

PL = [6500.0, 8500.0, 11500.0, 14000.0]  # 平台层（节间边界）


def _bar(bid, f, t, **props):
    b = {"id": bid, "from": f, "to": t, "role": "LEG"}
    b.update(props)
    return b


class LegChainStitchTest(unittest.TestCase):
    def test_same_panel_fragments_merge(self):
        """同节间共线碎片 → 合成 1 根整段。"""
        nodes = {
            "n1": (2258, 2258, 9000), "n2": (2200, 2200, 9830),
            "n3": (2180, 2180, 10043), "n4": (2130, 2130, 11041),
        }
        bars = [
            _bar("l1", "n1", "n2", bar_id="108"),
            _bar("l2", "n2", "n3"),
            _bar("l3", "n3", "n4"),
        ]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=PL)
        self.assertEqual(rep["merged_groups"], 1)
        # 三段并一，杆数 3 → 1
        self.assertEqual(len(out), 1)
        nb = out[0]
        # 复用链端现存节点：from=最低段底、to=最高段顶
        self.assertEqual(nb["from"], "n1")
        self.assertEqual(nb["to"], "n4")
        self.assertEqual(nb["geometry_origin"], "leg_chain_stitch")
        # bar_id 剥离 + 证据链
        self.assertNotIn("bar_id", nb)
        self.assertEqual(nb["source_bar_ids"], ["108"])
        self.assertEqual(nb["leg_stitched_n"], 3)

    def test_panel_boundary_breaks_chain(self):
        """平台层（8500/11500）边界必断——两节间不合并。"""
        nodes = {
            "n1": (2258, 2258, 9000), "n2": (2200, 2200, 9830),
            "n3": (2100, 2100, 12000), "n4": (2050, 2050, 13000),
        }
        bars = [
            _bar("l1", "n1", "n2"),   # 节间 [8500,11500)
            _bar("l2", "n3", "n4"),   # 节间 [11500,14000)
        ]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=PL)
        # gap 9830→12000 = 2170 > 400 且跨节间 → 不合并
        self.assertEqual(rep["merged_groups"], 0)
        self.assertEqual(len(out), 2)

    def test_degree_guard_splits_chain(self):
        """中间节点有外部杆挂接（度数>2）→ 在该节点断链。"""
        nodes = {
            "n1": (2258, 2258, 9000), "n2": (2200, 2200, 9830),
            "n3": (2180, 2180, 10043), "n4": (2130, 2130, 11041),
            "nX": (0, 1957, 9830),  # 横隔杆另一端
        }
        bars = [
            _bar("l1", "n1", "n2"),
            _bar("l2", "n2", "n3"),   # n2 度数 3（l1+l2+横隔）
            _bar("l3", "n3", "n4"),
            {"id": "ring", "from": "n2", "to": "nX",
             "role": "HORIZ", "diaphragm": True},
        ]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=PL)
        # n2 有横隔挂接 → l1 单独；l2+l3 合并
        self.assertEqual(rep["merged_groups"], 1)
        ids = [b["id"] for b in out]
        self.assertIn("l1", ids)
        merged = [b for b in out if str(b["id"]).startswith("legchain_")]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["from"], "n2")
        self.assertEqual(merged[0]["to"], "n4")
        # 横隔杆保留不动
        self.assertIn("ring", ids)

    def test_duplicate_fragments_dedup(self):
        """同角同段两来源重复 → 去重保证据强的。"""
        nodes = {
            "n1": (2258, 2258, 9000), "n2": (2200, 2200, 9830),
            "m1": (2257, 2257, 9010), "m2": (2201, 2201, 9840),
        }
        bars = [
            _bar("l_dxf", "n1", "n2", geometry_origin="dxf_geom",
                 geometry_class="recognized"),
            _bar("l_der", "m1", "m2", geometry_origin="derived_4face",
                 geometry_class="derived"),
        ]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=PL)
        # 重复段去掉 l_der，l_dxf 保留（单段不合并）
        self.assertEqual(rep["dropped_duplicates"], 1)
        ids = [b["id"] for b in out]
        self.assertIn("l_dxf", ids)
        self.assertNotIn("l_der", ids)

    def test_geometry_class_inherit(self):
        """全部源 recognized 才继承 recognized。"""
        nodes = {"n1": (2258, 2258, 9000), "n2": (2200, 2200, 9830),
                 "n3": (2130, 2130, 11041)}
        bars = [
            _bar("l1", "n1", "n2", geometry_class="recognized"),
            _bar("l2", "n2", "n3", geometry_class="derived"),
        ]
        out, _ = stitch_leg_chains(nodes, bars, panel_levels=PL)
        self.assertEqual(out[0]["geometry_class"], "derived")

    def test_angle_guard(self):
        """不共线（夹角>6°）→ 不合并。"""
        nodes = {
            "n1": (2258, 2258, 9000), "n2": (2200, 2200, 9830),
            "n3": (1600, 1600, 10600),  # 斜率突变
        }
        bars = [_bar("l1", "n1", "n2"), _bar("l2", "n2", "n3")]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=PL)
        self.assertEqual(rep["merged_groups"], 0)
        self.assertEqual(len(out), 2)

    def test_gap_guard(self):
        """真开口缝隙超 gap_mm → 不合并。

        共享节点的相邻碎片 gap=0（合法直连，见 test_same_panel）；
        本例是开口断缝：l1 顶 9400 与 l2 底 10200 无共享节点，
        3D 端点距 ~800mm > 400 → 拒绝合并。
        """
        nodes = {
            "n1": (2258, 2258, 8600), "n2": (2250, 2250, 9400),
            "n3": (2200, 2200, 10200), "n4": (2180, 2180, 10900),
        }
        bars = [_bar("l1", "n1", "n2"), _bar("l2", "n3", "n4")]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=PL)
        self.assertEqual(rep["merged_groups"], 0)
        self.assertEqual(len(out), 2)

    def test_no_panel_levels_noop(self):
        """无 panel_levels → 原样返回。"""
        nodes = {"n1": (2258, 2258, 9000), "n2": (2200, 2200, 9830)}
        bars = [_bar("l1", "n1", "n2")]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=None)
        self.assertEqual(out, bars)
        self.assertEqual(rep["merged_groups"], 0)

    def test_outside_panel_noop(self):
        """平台层外（如基座 0~6500）不合并——保守。"""
        nodes = {
            "n1": (2800, 2800, 1000), "n2": (2700, 2700, 3000),
            "n3": (2600, 2600, 5000),
        }
        bars = [_bar("l1", "n1", "n2"), _bar("l2", "n2", "n3")]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=PL)
        self.assertEqual(rep["merged_groups"], 0)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
