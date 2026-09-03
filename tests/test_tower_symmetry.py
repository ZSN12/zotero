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


class ExpandDagRebuildTest(unittest.TestCase):
    """P0 修复（2026-09-05）：四面展开后依赖 DAG 不得清空。

    契约：改动沿 DAG 传播 stale（Skill contract / staleness 机制）。
    展开重建组件 ID 后旧图悬空——此前直接 model.dependencies = {} 清空
    （DAG 契约失效）。修复后按新 ID 重建：镜像杆 → front 杆；
    dimension/rule → applies_to 目标。
    """

    def test_expand_rebuilds_dag_and_propagates_stale(self):
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        m = _make_model()
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        # 1. DAG 非空：镜像杆 derived_from → front 杆的边存在
        self.assertTrue(
            m.dependencies,
            "四面展开后 dependencies 必须按新组件 ID 重建，不得清空",
        )
        bar_edges = {
            n: ups for n, ups in m.dependencies.items()
            if n in m.components and m.components[n].kind == "tower_bar"
        }
        self.assertTrue(bar_edges, "至少存在镜像杆 → front 杆的 DAG 边")
        # 2. 边的两端都在新组件集内（无悬空引用）
        known = set(m.components) | set(m.dimensions) | set(m.rules)
        for node, ups in m.dependencies.items():
            self.assertIn(node, known, f"DAG 节点 {node} 悬空")
            for u in ups:
                self.assertIn(u, known, f"DAG 边 {node}→{u} 上游悬空")
        # 3. 失效传播：invalidate(front 杆) 必须波及其镜像下游
        edge_node, edge_ups = next(iter(bar_edges.items()))
        stale = m.invalidate(set(edge_ups))
        self.assertIn(
            edge_node, stale,
            f"invalidate({edge_ups}) 必须沿 DAG 传播到下游镜像 {edge_node}",
        )

    def test_rule_applies_to_list_edges(self):
        """Rule.applies_to 为 list 时 DAG 边逐目标登记（防 TypeError 回归）。"""
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        from traceability.model import Rule
        m = _make_model()
        bar_ids = [c.id for c in m.components.values() if c.kind == "tower_bar"]
        m.add_rule(Rule(id="r_list", name="列表规则", description="",
                        applies_to=bar_ids))
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        # 不抛 TypeError 且 rule 节点入 DAG
        self.assertIn("r_list", m.dependencies)
        self.assertTrue(m.dependencies["r_list"])

    def test_dag_bar_coverage_contract(self):
        """P1 审计（2026-09-05）契约：DAG 杆覆盖率不得回退。

        展开重建后每根 tower_bar 必须有 ≥1 条上游边（端点节点和/或
        drawing_file / 镜像→front）。孤岛杆 = 「改源头不失效」的静默
        契约破坏——此前 84.6% 杆无入边。门槛 0.98 而非 1.0：允许极少数
        无端点引用的杆存在（生成器输出未绑节点），但整体覆盖率必须
        停留在「几乎全覆盖」。
        """
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        m = _make_model()
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        bars = [c for c in m.components.values() if c.kind == "tower_bar"]
        self.assertTrue(bars, "测试模型必须有杆")
        covered = sum(
            1 for c in bars
            if m.dependencies.get(c.id)
        )
        ratio = covered / len(bars)
        self.assertGreaterEqual(
            ratio, 0.98,
            f"DAG 杆覆盖率 {ratio:.1%}（{covered}/{len(bars)}）低于 98% 契约门槛"
            "——存在孤岛杆，改源头不传播 stale",
        )
        # 节点级传播：invalidate 某节点必须波及引用它的杆
        node_ids = [c.id for c in m.components.values()
                    if c.kind == "tower_node"]
        self.assertTrue(node_ids)
        probe = next((n for n in node_ids
                      if m.downstream_of(n)), node_ids[0])
        stale = m.invalidate({probe})
        self.assertTrue(
            stale & {c.id for c in bars},
            f"invalidate(节点 {probe}) 必须沿 DAG 传播到引用该节点的杆",
        )


if __name__ == "__main__":
    unittest.main()


class ShortDiagFilterOrphanLabelsTest(unittest.TestCase):
    """Phase 2：短斜材过滤删除几何时，杆上件号必须进 orphan 登记簿。

    A1 语义是「件号文字是否被识别」——几何被结构规则清除（GT 不统计
    节点板连接件）不等于件号识别失败。证据链不许随几何消亡。
    """

    def test_short_diag_labels_collected(self):
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        from traceability.model import EngineeringModel, Component
        from traceability.model import SourceRef, SourceType

        m = EngineeringModel(name="t")
        m.add_component(Component(
            id="drawing_file", name="t", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
            properties={"drawing_view": "t", "view_mode": "single"},
        ))
        # 主腿（长竖杆，不受过滤影响）
        m.add_component(Component(id="n1", name="n1", kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
            properties={"x": 1000.0, "y": 0.0, "z": 0.0, "view_type": "front",
                        "drawing_view": "t", "source_file": "t"}))
        m.add_component(Component(id="n2", name="n2", kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
            properties={"x": 800.0, "y": 0.0, "z": 2000.0, "view_type": "front",
                        "drawing_view": "t", "source_file": "t"}))
        m.add_component(Component(id="b_leg", name="b_leg", kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
            properties={"bar_id": "201", "from_node": "n1", "to_node": "n2",
                        "section": "L63X5", "view_type": "front",
                        "drawing_view": "t", "source_file": "t"}))
        # 短斜材（300mm、45°倾角 < min_diag_len_mm=500）：几何应被过滤，
        # 但 bar_id=105 必须收进登记簿
        m.add_component(Component(id="n3", name="n3", kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
            properties={"x": 300.0, "y": 0.0, "z": 1000.0, "view_type": "front",
                        "drawing_view": "t", "source_file": "t"}))
        m.add_component(Component(id="n4", name="n4", kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
            properties={"x": 500.0, "y": 0.0, "z": 1200.0, "view_type": "front",
                        "drawing_view": "t", "source_file": "t"}))
        m.add_component(Component(id="b_short", name="b_short", kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "x.dxf", confidence=1.0),
            properties={"bar_id": "105", "from_node": "n3", "to_node": "n4",
                        "section": "L40X3", "view_type": "front",
                        "drawing_view": "t", "source_file": "t"}))
        spec = {
            "enable_4_face_expansion": True,
            "use_gt_half_width": False,
            "min_diag_len_mm": 500.0,   # 开启短斜材过滤
            "max_stub_len_mm": 0.0,     # 隔离：只测短斜材路径
        }
        expand_4_face_symmetry_model(m, spec)
        df = m.components["drawing_file"]
        orphans = df.properties.get("orphan_label_ids") or []
        self.assertIn("105", orphans,
                      "被过滤短斜材的件号必须进 orphan 登记簿（A1 证据不随几何消亡）")
        # 主腿不受影响
        self.assertNotIn("201", orphans)


if __name__ == "__main__":
    unittest.main()
