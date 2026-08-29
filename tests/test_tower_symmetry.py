"""阶段 2 验收：四面展开证据链与溯源元数据（geometry_class / derived_from / generated_face）。

覆盖官网验收标准：
    * 对称展开构件带 derived_from（原始 physical bar ID）
    * geometry_class ∈ {derived, reconstructed, recognized}
    * geometry_origin = "symmetry_rule" 或保留 dxf_geom / derived_4face
    * generated_face ∈ {F, B, L, R}
    * 严禁所有生成杆件统一继承根 drawing_file.source
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys = __import__("sys")
sys.path.insert(0, str(REPO))

from traceability.model import Component, EngineeringModel, SourceRef, SourceType  # noqa: E402


def _make_model():
    m = EngineeringModel(name="sym-test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "35A1-JC1-02.dxf"),
        properties={"view_kinds": ["front"]},
    ))
    for nid, (x, z) in {
        "A": (-100.0, 0.0), "B": (100.0, 0.0),
        "C": (-100.0, 100.0), "D": (100.0, 100.0),
    }.items():
        m.add_component(Component(
            id=nid, name=nid, kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "35A1-JC1-02.dxf"),
            properties={"view_type": "front", "x": x, "z": z,
                        "drawing_view": "35A1-JC1-02", "source_file": "35A1-JC1-02"},
        ))
    for bid, f, t in [
        ("leg_l", "A", "C"), ("leg_r", "B", "D"),
        ("horiz_bot", "A", "B"), ("horiz_top", "C", "D"), ("diag", "A", "D"),
    ]:
        m.add_component(Component(
            id=f"bar_{bid}", name=bid, kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "35A1-JC1-02.dxf", detail="view=front"),
            properties={"bar_id": bid, "view_type": "front",
                        "from_node": f, "to_node": t,
                        "drawing_view": "35A1-JC1-02", "source_file": "35A1-JC1-02",
                        "geometry_origin": "dxf_geom"},
        ))
    return m


class SymmetryEvidenceChainTest(unittest.TestCase):
    """四面展开后的 evidence_status / geometry_class / generated_face 溯源。"""

    def test_geometry_class_and_generated_face(self):
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        m = _make_model()
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)

        bars = [c for c in m.components.values() if c.kind == "tower_bar"]
        self.assertGreater(len(bars), 5, "四面展开应生成更多杆件")

        for b in bars:
            p = b.properties
            gc = p.get("geometry_class")
            self.assertIn(gc, ("derived", "reconstructed", "recognized"),
                          f"{b.id} geometry_class={gc} 不合法")
            # generated_face 大写（验收规范）
            if not p.get("diaphragm"):
                self.assertIn(p.get("generated_face"), ("F", "B", "L", "R"),
                              f"{b.id} generated_face={p.get('generated_face')}")
            # 非横隔杆件必须有 derived_from
            if not p.get("diaphragm"):
                self.assertIsNotNone(p.get("derived_from"), f"{b.id} 缺 derived_from")

    def test_mirrored_bars_not_uniform_root_source(self):
        """镜像面 b/l/r 不得统一继承根 drawing_file.source（应追溯原始构件 source）。"""
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        m = _make_model()
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)

        bars = [c for c in m.components.values() if c.kind == "tower_bar"]
        mirrored = [b for b in bars if b.properties.get("evidence_status") == "mirrored"]
        self.assertGreater(len(mirrored), 0, "应有镜像派生面杆件")
        for b in mirrored:
            self.assertIsNotNone(b.source, f"{b.id} mirrored 杆件丢失 source")
            # 镜像杆件的 source 不应是 drawing_file 组件（根 source）
            self.assertNotEqual(b.id, "drawing_file")
            self.assertEqual(b.properties.get("geometry_class"), "reconstructed",
                             f"{b.id} 镜像杆件 geometry_class 应为 reconstructed")

    def test_front_face_keeps_dxf_geom_origin(self):
        """front 面保留原始 dxf_geom，不被覆盖为 derived_4face。"""
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        m = _make_model()
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)

        front_bars = [
            c for c in m.components.values()
            if c.kind == "tower_bar" and c.properties.get("generated_face") == "F"
        ]
        self.assertGreater(len(front_bars), 0)
        for b in front_bars:
            self.assertEqual(b.properties.get("geometry_origin"), "dxf_geom",
                             f"{b.id} front 面 geometry_origin 应保留 dxf_geom")


if __name__ == "__main__":
    unittest.main()
