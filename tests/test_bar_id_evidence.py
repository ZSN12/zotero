"""阶段 4.4 验收：件号证据（bar_id_evidence）的写入与传播。

覆盖：
    * DXF 路径：extract_tower_from_dxf 的有号杆必须带 bar_id_evidence
      （sheet_id / label_component_id / text / association_method / distance /
        distance_unit / confidence），无号杆不带该字段（诚实表达「无证据」）。
    * 扫描路径：_associate_labels 的 assignment 携带 bar_id_evidence。
    * 四面展开：front 面保留原证据；镜像/派生面必须标记 symmetry propagation
      （propagated_via= symmetry_4face + propagated_face），不得冒充四次独立识别。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.model import Component, EngineeringModel, SourceRef, SourceType  # noqa: E402


def _mini_dxf_model():
    """两根杆 + 两个文字（视图推断需 ≥2 根杆），走真实 DXF 解析路径。"""
    import ezdxf

    d = tempfile.mkdtemp()
    p = Path(d) / "t.dxf"
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "1"})
    msp.add_line((200, 0), (300, 0), dxfattribs={"layer": "1"})
    msp.add_text("M0001", dxfattribs={"layer": "0", "height": 10}).set_placement((50, 0))
    msp.add_text("M0002", dxfattribs={"layer": "0", "height": 10}).set_placement((80, 0))
    doc.saveas(p)

    from traceability.intake.tower_dxf import extract_tower_from_dxf

    return extract_tower_from_dxf(p, layer_map={
        "bar_layers": ["1"], "text_layers": ["0"],
        "node_layers": ["1"], "dim_layers": [],
    })


class DxfBarIdEvidenceTest(unittest.TestCase):
    """DXF 路径：有号杆的 bar_id_evidence 自包含可追溯。"""

    def test_labeled_bar_has_evidence_fields(self):
        model = _mini_dxf_model()
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        self.assertEqual(len(bars), 2)
        by_id = {b.properties.get("bar_id"): b for b in bars}
        self.assertIn("M0001", by_id)

        ev_list = by_id["M0001"].properties.get("bar_id_evidence")
        self.assertIsInstance(ev_list, list)
        self.assertEqual(len(ev_list), 1)
        ev = ev_list[0]
        for key in ("sheet_id", "label_component_id", "text",
                    "association_method", "distance", "distance_unit", "confidence"):
            self.assertIn(key, ev, f"evidence 缺字段 {key}")
        self.assertEqual(ev["text"], "M0001")
        self.assertEqual(ev["association_method"], "nearest_text_same_view_greedy")
        self.assertEqual(ev["distance_unit"], "drawing")
        self.assertIsInstance(ev["distance"], float)

    def test_evidence_distance_matches_label_distance(self):
        model = _mini_dxf_model()
        bars = [c for c in model.components.values() if c.kind == "tower_bar"]
        for b in bars:
            ev = b.properties.get("bar_id_evidence") or []
            if not ev:
                continue
            self.assertAlmostEqual(
                float(ev[0]["distance"]),
                float(b.properties.get("label_distance")),
                places=2,
            )


class ScanAssociateEvidenceTest(unittest.TestCase):
    """扫描路径：assignment 携带 bar_id_evidence。"""

    def test_associate_labels_attaches_evidence(self):
        from traceability.intake.tower_agent_pipeline import _associate_labels

        bars = [
            {"bar_uid": "bar_0000", "x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 0.0,
             "view_type": "front"},
            {"bar_uid": "bar_0001", "x1": 0.0, "y1": 500.0, "x2": 100.0, "y2": 500.0,
             "view_type": "front"},
        ]
        labels = [
            {"text": "M0001", "x_px": 50.0, "y_px": 5.0, "view_type": "front"},
        ]
        res = _associate_labels(bars, labels, snap_px=50.0)
        assigns = {a["bar_uid"]: a for a in res["assignments"]}
        ev = assigns["bar_0000"].get("bar_id_evidence")
        self.assertIsInstance(ev, list)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["text"], "M0001")
        self.assertEqual(ev[0]["association_method"], "nearest_midpoint_same_view_greedy")
        self.assertEqual(ev[0]["distance_unit"], "px")
        # 未贴号杆：空列表（诚实表达「无证据」），不是缺失
        self.assertEqual(assigns["bar_0001"].get("bar_id_evidence"), [])


def _make_model_with_evidence():
    m = EngineeringModel(name="ev-sym-test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "t.dxf"),
        properties={"view_kinds": ["front"]},
    ))
    for nid, (x, z) in {
        "A": (-100.0, 0.0), "B": (100.0, 0.0),
        "C": (-100.0, 100.0), "D": (100.0, 100.0),
    }.items():
        m.add_component(Component(
            id=nid, name=nid, kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties={"view_type": "front", "x": x, "z": z,
                        "drawing_view": "t", "source_file": "t"},
        ))
    evidence = [{
        "sheet_id": "t",
        "label_component_id": "text_31",
        "text": "M0001",
        "association_method": "nearest_text_same_view_greedy",
        "distance": 12.5,
        "distance_unit": "drawing",
        "confidence": 0.85,
    }]
    for bid, f, t in [("leg_l", "A", "C"), ("leg_r", "B", "D"), ("horiz_bot", "A", "B")]:
        m.add_component(Component(
            id=f"bar_{bid}", name=bid, kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "t.dxf", detail="view=front"),
            properties={"bar_id": "M0001" if bid == "leg_l" else bid,
                        "view_type": "front",
                        "from_node": f, "to_node": t,
                        "drawing_view": "t", "source_file": "t",
                        "geometry_origin": "dxf_geom",
                        "bar_id_evidence": [dict(evidence[0])]},
        ))
    return m


class SymmetryPropagationMarkTest(unittest.TestCase):
    """四面展开：镜像面证据必须带 symmetry propagation 标记。"""

    def test_mirrored_face_marks_propagation(self):
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model

        m = _make_model_with_evidence()
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        bars = [c for c in m.components.values() if c.kind == "tower_bar"]
        self.assertTrue(bars)

        for b in bars:
            p = b.properties
            ev = p.get("bar_id_evidence") or []
            face = str(p.get("face") or "").lower()
            if face == "f":
                # front 面是识别原貌：证据原样保留，不带传播标记
                for e in ev:
                    self.assertNotIn("propagated_via", e)
            else:
                # 镜像/派生面：这是传播，必须显式标记
                for e in ev:
                    self.assertEqual(e.get("propagated_via"), "symmetry_4face")
                    self.assertEqual(e.get("propagated_face"), p.get("generated_face"))

    def test_propagation_does_not_mutate_source_evidence(self):
        """展开后源杆（front 面）的证据对象不得被镜像面共享（深拷贝防污染）。"""
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model

        m = _make_model_with_evidence()
        src = next(c for c in m.components.values()
                   if c.kind == "tower_bar" and c.id == "bar_leg_l")
        src_ev = src.properties["bar_id_evidence"]
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        # 源证据（若源组件被保留）或 front 面副本都不应带 propagated_via
        front_bars = [c for c in m.components.values()
                      if c.kind == "tower_bar"
                      and str(c.properties.get("face") or "").lower() == "f"]
        self.assertTrue(front_bars)
        for c in front_bars:
            for e in (c.properties.get("bar_id_evidence") or []):
                self.assertNotIn("propagated_via", e)


if __name__ == "__main__":
    unittest.main()
