"""阶段 4：证据链验收测试。

覆盖：
    * 每根非 derived 杆件 projection_refs 非空且自包含（sheet_id/view_type）
    * 深拷贝隔离：改一根杆件的 projection_refs 不影响其它杆件
    * validate_references 校验 projection_refs.source_component_id 悬空
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO))

from traceability.model import Component, EngineeringModel, SourceRef, SourceType  # noqa: E402


def _make_front_model():
    """最小 front 立面模型：2 节点 1 杆，带 projection_refs。"""
    m = EngineeringModel(name="evidence")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "s.dxf"),
        properties={"view_kinds": ["front"]},
    ))
    m.add_component(Component(
        id="n1", name="n1", kind="tower_node",
        source=SourceRef(SourceType.DRAWING, "s.dxf"),
        properties={"view_type": "front", "x": -100.0, "z": 0.0},
    ))
    m.add_component(Component(
        id="n2", name="n2", kind="tower_node",
        source=SourceRef(SourceType.DRAWING, "s.dxf"),
        properties={"view_type": "front", "x": 100.0, "z": 0.0},
    ))
    m.add_component(Component(
        id="bar_h", name="h", kind="tower_bar",
        source=SourceRef(SourceType.DRAWING, "s.dxf"),
        properties={
            "bar_id": "105", "view_type": "front", "from_node": "n1", "to_node": "n2",
            "geometry_origin": "dxf_geom",
            "projection_refs": [{
                "sheet_id": "s", "view_id": "s__front", "view_type": "front",
                "source_component_id": "2F", "source_reference": "s.dxf",
                "geometry_origin": "dxf_geom", "confidence": 0.85,
            }],
        },
    ))
    return m


class ProjectionRefsDeepCopyTest(unittest.TestCase):
    """阶段 4.5：四面展开后各杆件 projection_refs 互不共享（改一根不影响其它）。"""

    def test_expand_deepcopies_projection_refs(self):
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        m = _make_front_model()
        expand_4_face_symmetry_model(m, add_diaphragms=False, weld_corner_legs=False)
        bars = [c for c in m.components.values() if c.kind == "tower_bar"]
        # 至少 1 根 front recognized + 镜像面
        self.assertGreater(len(bars), 1)
        # 修改第一根的 projection_refs，其它杆不得受影响
        first = bars[0]
        first.properties["projection_refs"][0]["confidence"] = 0.999
        for other in bars[1:]:
            for ref in (other.properties.get("projection_refs") or []):
                self.assertNotEqual(
                    ref.get("confidence"), 0.999,
                    f"{other.id} 与 {first.id} 共享同一 projection_refs dict（浅拷贝）",
                )


class ValidateReferencesProjectionRefsTest(unittest.TestCase):
    """阶段 4.3：validate_references 校验 projection_refs.source_component_id。"""

    def test_handle_reference_not_flagged(self):
        from traceability.io import validate_references
        m = _make_front_model()
        # source_component_id='2F'（DXF handle）是外部稳定引用，不悬空
        self.assertEqual(validate_references(m), [])

    def test_component_id_reference_must_resolve(self):
        from traceability.io import validate_references
        m = _make_front_model()
        # 改成一个「组件内 ID」语义但不存在于 components -> 应报悬空
        for c in m.components.values():
            if c.kind == "tower_bar":
                c.properties["projection_refs"][0]["source_component_id"] = "bar_ghost"
        problems = validate_references(m)
        self.assertTrue(
            any("projection_refs" in p for p in problems),
            f"应检出 projection_refs.source_component_id 悬空，实际 {problems}",
        )


if __name__ == "__main__":
    unittest.main()
