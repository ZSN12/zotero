# -*- coding: utf-8 -*-
"""P4.3：件号长度一致性核验（错配剥离）回归测试。"""
import unittest


def _mk_model(bars_spec, bom):
    """bars_spec: [(bar_id, length_mm_3d)]；bom: {bar_id: length}。"""
    from traceability.model import EngineeringModel, Component, Dimension
    model = EngineeringModel(name="t", version="1")
    for i, (bid, L) in enumerate(bars_spec):
        cid = f"4f_stem{i}_F"
        model.components[cid] = Component(
            id=cid, name=cid, kind="tower_bar",
            properties={"bar_id": str(bid), "length_mm_3d": float(L), "face": "f"})
    for bid, L in bom.items():
        model.dimensions[f"dim_bom_length_{bid}"] = Dimension(
            id=f"dim_bom_length_{bid}", name=f"bom_len_{bid}",
            value=float(L), unit="mm")
    model.components["drawing_file"] = Component(
        id="drawing_file", name="drawing_file", kind="drawing_file", properties={})
    return model


class BarIdMisassociationTest(unittest.TestCase):
    def _run(self, bars_spec, bom):
        from traceability.intake.tower_symmetry import _strip_misassociated_bar_ids
        model = _mk_model(bars_spec, bom)
        _strip_misassociated_bar_ids(model, strip_ratio=2.5, suspect_ratio=1.03)
        return model

    def test_strips_gross_mismatch(self):
        """ratio > 2.5：件号剥离 + orphan 登记。"""
        m = self._run([(337, 1153.0)], {337: 340.0})
        p = m.components["4f_stem0_F"].properties
        self.assertIsNone(p["bar_id"])
        self.assertEqual(p["bar_id_detached"], "337")
        self.assertTrue(p["bar_id_misassociation"])
        df = m.components["drawing_file"].properties
        self.assertIn("337", df["orphan_label_ids"])
        self.assertIn("337", df["bar_id_misassociated_stripped"])

    def test_suspect_keeps_bar_id(self):
        """1.03 < ratio <= 2.5：保留件号，标 suspect。"""
        m = self._run([(112, 1800.0)], {112: 913.0})
        p = m.components["4f_stem0_F"].properties
        self.assertEqual(p["bar_id"], "112")
        self.assertTrue(p.get("bar_id_length_suspect"))
        self.assertNotIn("bar_id_detached", p)

    def test_underlength_not_stripped(self):
        """ratio < 0.4（识别不全）：不剥离——几何问题是 Phase 5/7 战场。"""
        m = self._run([(304, 73.0)], {304: 2959.0})
        p = m.components["4f_stem0_F"].properties
        self.assertEqual(p["bar_id"], "304")

    def test_compliant_passthrough(self):
        """ratio in [0.97, 1.03]：无任何标记。"""
        m = self._run([(5, 1000.0)], {5: 1000.0})
        p = m.components["4f_stem0_F"].properties
        self.assertEqual(p["bar_id"], "5")
        self.assertFalse(p.get("bar_id_length_suspect"))
        self.assertNotIn("bar_id_detached", p)

    def test_no_bom_dimension_skipped(self):
        """无 BOM 尺寸的杆：跳过（无核验依据）。"""
        m = self._run([(999, 1500.0)], {})
        p = m.components["4f_stem0_F"].properties
        self.assertEqual(p["bar_id"], "999")


if __name__ == "__main__":
    unittest.main()
