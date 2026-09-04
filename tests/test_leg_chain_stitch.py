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


class LegSynthExemptTest(unittest.TestCase):
    """P2.5（2026-09-05，6b7831b）：leg_synth 表驱动跨型杆豁免链合并。

    真实病灶（06 册实测）：双拼角钢两链分段边界差 100mm
    （(14400,17000) vs (14500,17000)），链内去重 dup_mid_tol_mm=120
    把中点距 ~51mm 的后到者当重复删除；(14000,16000)+(14000,17000)
    被重叠合并成跨层长杆。表驱动分段已完整（overlay 披露的
    leg_synth_spans_mm），跳过链合并保留证据杆原貌。
    豁免计数进 report["skipped"]（B2 可审计要求）。
    """

    def test_leg_synth_twin_spans_both_kept(self):
        """双拼邻段 (14400,17000)/(14500,17000) 都保留——豁免去重误杀。"""
        nodes = {
            "a1": (1760, 1760, 14400), "a2": (1580, 1580, 17000),
            "b1": (1750, 1750, 14500), "b2": (1575, 1575, 17000),
        }
        bars = [
            _bar("tA", "a1", "a2", geometry_origin="leg_synth"),
            _bar("tB", "b1", "b2", geometry_origin="leg_synth"),
        ]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=[6500, 14400, 17000])
        # 两根都原样保留：不去重、不合并、属性不变
        self.assertEqual(len(out), 2)
        ids = {b["id"] for b in out}
        self.assertEqual(ids, {"tA", "tB"})
        # 豁免计数落报告（可审计）
        self.assertEqual(rep.get("skipped", {}).get("leg_synth_table"), 2)

    def test_leg_synth_not_merged_with_dxf_fragments(self):
        """leg_synth 杆不与同角 dxf 腿碎段合并（重叠链合并豁免）。"""
        nodes = {
            "a1": (1760, 1760, 14000), "a2": (1580, 1580, 17000),
            "f1": (1755, 1755, 14000), "f2": (1700, 1700, 15000),
            "f3": (1660, 1660, 15900), "f4": (1590, 1590, 16900),
        }
        bars = [
            _bar("tab", "a1", "a2", geometry_origin="leg_synth"),
            _bar("fr1", "f1", "f2"), _bar("fr2", "f2", "f3"),
            _bar("fr3", "f3", "f4"),
        ]
        out, rep = stitch_leg_chains(nodes, bars, panel_levels=[6500, 14000, 17000])
        # 表杆原样保留
        ids = {b["id"]: b for b in out}
        self.assertIn("tab", ids)
        self.assertEqual(ids["tab"]["from"], "a1")
        self.assertEqual(ids["tab"]["to"], "a2")
        # dxf 碎段正常合并（豁免不影响常规通道）
        self.assertEqual(rep["merged_groups"], 1)
        self.assertEqual(rep.get("skipped", {}).get("leg_synth_table"), 1)

    def test_leg_synth_exempt_audit_counter_reported(self):
        """豁免计数进报告——跨口径可审计（B2 修复要求）。"""
        nodes = {"a1": (1760, 1760, 14400), "a2": (1580, 1580, 17000)}
        bars = [_bar("tA", "a1", "a2", geometry_origin="leg_synth")]
        _, rep = stitch_leg_chains(nodes, bars, panel_levels=[6500, 14400, 17000])
        self.assertEqual(rep.get("skipped", {}).get("leg_synth_table"), 1)
