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


class FitHalfWidthNotGtAlignedTest(unittest.TestCase):
    """阶段3.2回归：生产路径 fit 半宽不得误标 gt_aligned（评测会误拒）。"""

    def test_fit_half_width_does_not_mark_gt_aligned(self):
        import json
        from pathlib import Path
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        from traceability.io import load_model
        # 构造最小模型（含 front 主腿立面），生产 spec 不注入 GT
        # 直接检查 expand 后无 gt_aligned 标记
        import tempfile, shutil
        tmp = tempfile.mkdtemp()
        try:
            from traceability.model import EngineeringModel, Component
            from traceability.model import SourceRef, SourceType
            m = EngineeringModel(name="t")
            m.add_component(Component(
                id="drawing_file", name="t", kind="drawing_file",
                source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
                properties={"drawing_view": "t", "view_mode": "single"},
            ))
            # 主腿立面节点（3 个主腿端点，足够拟合半宽）
            m.add_component(Component(id="n1", name="n1", kind="tower_node",
                source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
                properties={"x": 1000.0, "y": 0.0, "z": 0.0, "view_type": "front",
                            "drawing_view": "t", "source_file": "t"}))
            m.add_component(Component(id="n2", name="n2", kind="tower_node",
                source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
                properties={"x": 800.0, "y": 0.0, "z": 1000.0, "view_type": "front",
                            "drawing_view": "t", "source_file": "t"}))
            m.add_component(Component(id="n3", name="n3", kind="tower_node",
                source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
                properties={"x": 600.0, "y": 0.0, "z": 2000.0, "view_type": "front",
                            "drawing_view": "t", "source_file": "t"}))
            m.add_component(Component(id="b1", name="b1", kind="tower_bar",
                source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
                properties={"bar_id": "105", "from_node": "n1", "to_node": "n2",
                            "section": "L40X3", "view_type": "front",
                            "drawing_view": "t", "source_file": "t"}))
            m.add_component(Component(id="b2", name="b2", kind="tower_bar",
                source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
                properties={"bar_id": "105", "from_node": "n2", "to_node": "n3",
                            "section": "L40X3", "view_type": "front",
                            "drawing_view": "t", "source_file": "t"}))
            spec = {"enable_4_face_expansion": True, "use_gt_half_width": False}
            expand_4_face_symmetry_model(m, spec)
            for cid, c in m.components.items():
                if c.kind in ("tower_bar", "tower_node"):
                    self.assertFalse(c.properties.get("gt_aligned"),
                                     f"{cid} 生产 fit 路径不得打 gt_aligned")
            df = m.components["drawing_file"]
            self.assertEqual(df.properties.get("half_width_source"), "fit")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class DerivedFromResolvableTest(unittest.TestCase):
    """阶段4.3：mirrored 杆件 derived_from 指向 front 面物理杆件（可解析）。"""

    def test_mirrored_derived_from_points_to_front(self):
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        from traceability.model import Component, EngineeringModel, SourceRef, SourceType
        m = EngineeringModel(name="t")
        m.add_component(Component(
            id="drawing_file", name="df", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "x.dxf"),
            properties={"view_kinds": ["front"]},
        ))
        # 4 节点矩形 + 3 段主腿（足够拟合半宽）
        for nid, (x, z) in {"A": (-100.0, 0.0), "B": (100.0, 0.0),
                            "C": (-80.0, 100.0), "D": (80.0, 100.0),
                            "E": (-60.0, 200.0), "F": (60.0, 200.0)}.items():
            m.add_component(Component(
                id=nid, name=nid, kind="tower_node",
                source=SourceRef(SourceType.DRAWING, "x.dxf"),
                properties={"view_type": "front", "x": x, "z": z,
                            "drawing_view": "t", "source_file": "t"},
            ))
        for bid, f, t in [("legL1", "A", "C"), ("legR1", "B", "D"),
                          ("legL2", "C", "E"), ("legR2", "D", "F")]:
            m.add_component(Component(
                id=f"bar_{bid}", name=bid, kind="tower_bar",
                source=SourceRef(SourceType.DRAWING, "x.dxf"),
                properties={"bar_id": bid, "view_type": "front",
                            "from_node": f, "to_node": t,
                            "drawing_view": "t", "source_file": "t",
                            "geometry_origin": "dxf_geom"},
            ))
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        comps = m.components
        all_ids = set(comps.keys())
        mirrored = [c for c in comps.values()
                    if c.kind == "tower_bar" and c.properties.get("geometry_class") == "reconstructed"]
        self.assertGreater(len(mirrored), 0, "应有 mirrored 杆件")
        for b in mirrored:
            df = b.properties.get("derived_from")
            self.assertIsNotNone(df, f"{b.id} 缺 derived_from")
            self.assertIn(df, all_ids, f"{b.id} derived_from '{df}' 应可解析（指向 front 物理杆件）")


class AppliesToRetargetTest(unittest.TestCase):
    """阶段4.6：四面展开后 rules/dimensions 的 applies_to 不得悬空（M0 门槛：悬空引用为 0）。"""

    def test_expand_retargets_rules_and_dimensions(self):
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        from traceability.io import validate_references
        from traceability.model import Dimension, DimensionOrigin, Rule
        m = _make_model()
        # 注入规则与尺寸，applies_to 指向展开前的旧 bar/node ID
        bar_ids = [c.id for c in m.components.values() if c.kind == "tower_bar"]
        node_ids = [c.id for c in m.components.values() if c.kind == "tower_node"]
        m.add_rule(Rule(
            id="r_topology_closed", name="拓扑闭合", description="",
            applies_to=bar_ids,
        ))
        m.add_rule(Rule(
            id="r_node_fully_solved", name="节点三轴齐备", description="",
            applies_to=node_ids,
        ))
        m.add_dimension(Dimension(
            id="dim_bom_length_leg_l", name="BOM 长度", value=100.0, unit="mm",
            origin=DimensionOrigin.MEASURED, applies_to="bar_leg_l",
        ))
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        problems = validate_references(m)
        self.assertEqual(
            problems, [],
            f"四面展开后 rules/dimensions 的 applies_to 不得悬空，实际 {len(problems)} 个：{problems[:5]}",
        )


if __name__ == "__main__":
    unittest.main()
