# -*- coding: utf-8 -*-
"""P5 约束残差（2026-09-03）回归测试：BOM 行分类 + sidegen P4.3 补跑 +
截面属性阶梯 + sidegen l/r 物理根数去重。

背景：r_bom_length_match / r_bom_section_match / r_project_bom_master
三个 FAILED 拦交付的根因链——
  1. guowang 合并 BOM 的板材/螺栓/mangled 行挤在同一 bar_id 命名空间，
     非杆件行建 dim_bom_* 维度拿螺栓行核 tower_bar 必然 FAILED；
  2. sidegen 杆在 expand 末尾的 P4.3 阶梯之后才注入，从不经过 strip/
     suspect 分级，中等超差直接 FAILED；
  3. 杆自带截面抄到材料表板材文字（'-6X40'）而 BOM member 行是角钢；
  4. sidegen l/r 孪生被 _root_stem 计成 2 根物理杆（大写后缀才剥），
     bar 122/140 数量 2>1 假冲突引爆图册级规则。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ClassifyBomRowTest(unittest.TestCase):
    def test_member_sections(self):
        from traceability.intake.tower_bom import classify_bom_row
        for sec in ("L40X3", "Q345L70X5", "q355L100X7", "16MNL56X5"):
            self.assertEqual(classify_bom_row("101", sec), "member", sec)

    def test_plate_bolt_mangled(self):
        from traceability.intake.tower_bom import classify_bom_row
        self.assertEqual(classify_bom_row("137", "-6X40"), "plate")
        self.assertEqual(classify_bom_row("151", "Q345-14X260"), "plate")
        self.assertEqual(classify_bom_row("316", "5M16X40"), "bolt")
        self.assertEqual(classify_bom_row("\\M+5B9E6", "50"), "mangled")
        self.assertEqual(classify_bom_row("-3(%%c17.5)", "X"), "mangled")
        self.assertEqual(classify_bom_row("Q345L63X5", "Q345L63X5"), "mangled")


class CrossCheckBomMemberOnlyTest(unittest.TestCase):
    def test_non_member_rows_get_no_dims(self):
        """非 member 行保留 bom_row 组件但不建交叉核验维度。"""
        from traceability.intake.tower_bom import cross_check_bom
        from traceability.model import EngineeringModel
        m = EngineeringModel(name="t", version="1")
        rows = [
            {"bar_id": "137", "section": "L40X3", "length_mm": 1200.0, "qty": 2},
            {"bar_id": "316", "section": "5M16X40", "length_mm": 336.0, "qty": 4},
            {"bar_id": "335", "section": "-6X128", "length_mm": 200.0, "qty": 1},
        ]
        cross_check_bom(m, rows)
        self.assertIn("bom_316", m.components)      # 数据不丢
        self.assertIn("bom_335", m.components)
        self.assertIn("dim_bom_length_137", m.dimensions)   # member 行建维度
        self.assertNotIn("dim_bom_length_316", m.dimensions)  # 螺栓行不建
        self.assertNotIn("dim_bom_section_335", m.dimensions)  # 垫板行不建
        self.assertEqual(
            m.components["bom_316"].properties["row_class"], "bolt")
        self.assertEqual(
            m.components["bom_335"].properties["row_class"], "plate")


class SidegenP43LadderTest(unittest.TestCase):
    """sidegen 杆补跑 P4.3 阶梯 + 截面属性阶梯。"""

    def _mk(self, *, bar_id, length, section, bom_len, bom_sec):
        from traceability.model import (
            EngineeringModel, Component, Dimension, DimensionOrigin)
        m = EngineeringModel(name="t", version="1")
        m.components["drawing_file"] = Component(
            id="drawing_file", name="df", kind="drawing_file", properties={
                "side_reads": [{
                    "from": [0.0, 0.0, 0.0],
                    "to": [float(length), 0.0, 0.0],
                    "bar_id": bar_id, "section": section,
                    "source_file": "s1", "confidence": 0.85,
                }]})
        m.components["b0"] = Component(
            id="b0", name="b0", kind="tower_bar", properties={
                "bar_id": bar_id, "section": section,
                "length_mm_3d": float(length)})
        m.dimensions[f"dim_bom_length_{bar_id}"] = Dimension(
            id=f"dim_bom_length_{bar_id}", name="l", value=float(bom_len),
            unit="mm", origin=DimensionOrigin.MEASURED)
        m.dimensions[f"dim_bom_section_{bar_id}"] = Dimension(
            id=f"dim_bom_section_{bar_id}", name="s", value=bom_sec,
            unit="", origin=DimensionOrigin.MEASURED)
        return m

    def test_sidegen_moderate_overlength_gets_suspect(self):
        """sidegen 中等超差（1.03-2.5×）→ suspect（PENDING 语义）。"""
        from traceability.intake.tower_views import apply_side_reads
        from traceability.intake.tower_bom import cross_check_bom
        m = self._mk(bar_id="110", length=1394.0, section="L40X3",
                     bom_len=1023.0, bom_sec="L40X3")
        cross_check_bom(m, [{"bar_id": "110", "section": "L40X3",
                             "length_mm": 1023.0, "qty": 8}])
        n = apply_side_reads(m)
        self.assertGreaterEqual(n, 1)
        for cid, c in m.components.items():
            if c.kind == "tower_bar" and "sidegen__" in cid:
                self.assertTrue(
                    c.properties.get("bar_id_length_suspect"),
                    f"{cid} 应被 P4.3 补跑标记 suspect")

    def test_sidegen_extreme_overlength_detaches_bar_id(self):
        """sidegen 极端错配（>2.5×）→ 件号剥离进 orphan。"""
        from traceability.intake.tower_views import apply_side_reads
        from traceability.intake.tower_bom import cross_check_bom
        m = self._mk(bar_id="154", length=441.7, section="-6X40",
                     bom_len=40.0, bom_sec="L40X3")
        cross_check_bom(m, [{"bar_id": "154", "section": "L40X3",
                             "length_mm": 40.0, "qty": 4}])
        apply_side_reads(m)
        df = m.components["drawing_file"].properties
        self.assertIn("154", df.get("orphan_label_ids") or [])
        for cid, c in m.components.items():
            if c.kind == "tower_bar" and "sidegen__" in cid:
                self.assertIsNone(c.properties.get("bar_id"))

    def test_plate_section_attribute_rehung_to_bom(self):
        """杆截面为板材形态、BOM member 行为角钢 → 属性按 BOM 重挂。"""
        from traceability.intake.tower_symmetry import _strip_misassociated_bar_ids
        from traceability.intake.tower_bom import cross_check_bom
        m = self._mk(bar_id="112", length=902.0, section="-6X128",
                     bom_len=913.0, bom_sec="L40X3")
        cross_check_bom(m, [{"bar_id": "112", "section": "L40X3",
                             "length_mm": 913.0, "qty": 8}])
        _strip_misassociated_bar_ids(m)
        p = m.components["b0"].properties
        self.assertEqual(p["section"], "L40X3")
        self.assertEqual(p["section_detached"], "-6X128")
        self.assertEqual(p["section_source"], "bom_member_row")
        df = m.components["drawing_file"].properties
        self.assertIn("112", (df.get("section_attribute_detached") or [""])[0])

    def test_angle_vs_angle_section_stays_untouched(self):
        """角钢对角钢的实质不符（同 class）不重挂——诚实失败信号保留。"""
        from traceability.intake.tower_symmetry import _strip_misassociated_bar_ids
        from traceability.intake.tower_bom import cross_check_bom
        m = self._mk(bar_id="115", length=1002.0, section="L63X5",
                     bom_len=927.0, bom_sec="L40X3")
        cross_check_bom(m, [{"bar_id": "115", "section": "L40X3",
                             "length_mm": 927.0, "qty": 4}])
        _strip_misassociated_bar_ids(m)
        p = m.components["b0"].properties
        self.assertEqual(p["section"], "L63X5")
        self.assertNotIn("section_detached", p)


class SidegenRootStemTest(unittest.TestCase):
    def test_sidegen_lr_twins_share_root_stem(self):
        from traceability.project.module_build import _root_stem
        self.assertEqual(_root_stem("sidegen__b0059_l"),
                         _root_stem("sidegen__b0059_r"))
        self.assertEqual(_root_stem("sidegen__b0059_l"), "sidegen__b0059")
        # 大写四面后缀语义不回归
        self.assertEqual(_root_stem("4f_x_F"), "x")
        # sidegen 非孪生后缀不误剥
        self.assertEqual(_root_stem("sidegen__b0059"), "sidegen__b0059")


class SectionValidatorSuspectPathTest(unittest.TestCase):
    """r_bom_section_match：件号 suspect 的杆截面矛盾进 review（PENDING）。"""

    def _mk(self, *, suspect, section, bom_sec):
        from traceability.model import (
            EngineeringModel, Component, Dimension, DimensionOrigin)
        m = EngineeringModel(name="t", version="1")
        m.components["b1"] = Component(
            id="b1", name="b1", kind="tower_bar", properties={
                "bar_id": "115", "section": section,
                "length_mm_3d": 1002.0,
                **({"bar_id_length_suspect": True} if suspect else {})})
        m.dimensions["dim_bom_section_115"] = Dimension(
            id="dim_bom_section_115", name="s", value=bom_sec,
            unit="", origin=DimensionOrigin.MEASURED)
        return m

    def test_suspect_bar_section_goes_pending(self):
        from traceability.harness.tower_validators import validate_bom_section_match
        m = self._mk(suspect=True, section="L63X5", bom_sec="L40X3")
        r = validate_bom_section_match(m, "r_bom_section_match")
        self.assertEqual(r.status.value, "pending")

    def test_non_suspect_section_mismatch_still_fails(self):
        from traceability.harness.tower_validators import validate_bom_section_match
        m = self._mk(suspect=False, section="L63X5", bom_sec="L40X3")
        r = validate_bom_section_match(m, "r_bom_section_match")
        self.assertEqual(r.status.value, "failed")


if __name__ == "__main__":
    unittest.main()
