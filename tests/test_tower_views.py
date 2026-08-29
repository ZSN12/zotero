"""阶段 2 验收：跨视图投影合并证据链（projection_refs / unresolved_projection_refs）。

覆盖官网验收标准：
    * 合并多视图投影生成物理杆件时，全部来源写入 projection_refs
      （含 sheet_id / view_type / component_id / confidence / geometry_origin）
    * 未被成功匹配的孤立投影写入 unresolved_projection_refs，不静默丢弃
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys = __import__("sys")
sys.path.insert(0, str(REPO))

from traceability.model import Component, EngineeringModel, SourceRef, SourceType  # noqa: E402


def _make_two_view_model(with_unresolved=False):
    m = EngineeringModel(name="views-test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "s.dxf"),
        properties={"view_kinds": ["front", "plan"]},
    ))
    for nid, (x, y, z) in {"N1": (0.0, 0.0, 0.0), "N2": (100.0, 0.0, 0.0)}.items():
        m.add_component(Component(
            id=nid, name=nid, kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "s.dxf"),
            properties={"view_type": "front", "x": x, "y": y, "z": z},
        ))
    m.add_component(Component(
        id="bar_front_1", name="bar1", kind="tower_bar",
        source=SourceRef(SourceType.DRAWING, "s.dxf", detail="view=front", confidence=0.9),
        properties={"bar_id": "105", "view_type": "front",
                    "from_node": "N1", "to_node": "N2",
                    "source_file": "s", "drawing_view": "s", "geometry_origin": "dxf_geom"},
    ))
    m.add_component(Component(
        id="bar_plan_1", name="bar1-plan", kind="tower_bar",
        source=SourceRef(SourceType.DRAWING, "s.dxf", detail="view=plan", confidence=0.8),
        properties={"bar_id": "105", "view_type": "plan",
                    "from_node": "N1", "to_node": "N2",
                    "source_file": "s", "drawing_view": "s", "geometry_origin": "dxf_geom"},
    ))
    if with_unresolved:
        # 孤立投影：件号与长度均无法匹配到主杆件（用独立节点构成不同长度）
        m.add_component(Component(
            id="N3", name="N3", kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "s.dxf"),
            properties={"view_type": "plan", "x": 0.0, "y": 0.0, "z": 0.0},
        ))
        m.add_component(Component(
            id="N4", name="N4", kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "s.dxf"),
            properties={"view_type": "plan", "x": 300.0, "y": 0.0, "z": 0.0},
        ))
        m.add_component(Component(
            id="bar_plan_orphan", name="bar-orphan", kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "s.dxf", detail="view=plan", confidence=0.7),
            properties={"bar_id": "999", "view_type": "plan",
                        "from_node": "N3", "to_node": "N4",
                        "source_file": "s", "drawing_view": "s"},
        ))
    return m


class MergeViewBarsEvidenceTest(unittest.TestCase):
    """P2-6：投影合并后 projection_refs 完整、孤立投影进入 unresolved。"""

    def test_projection_refs_contain_geometry_origin(self):
        from traceability.intake.tower_views import merge_view_bars
        m = _make_two_view_model()
        merge_view_bars(m)

        bars = [c for c in m.components.values() if c.kind == "tower_bar"]
        self.assertEqual(len(bars), 1, "plan 投影应合并到 front 主杆件")
        bar = bars[0]
        prs = bar.properties.get("projection_refs") or []
        self.assertGreaterEqual(len(prs), 1)
        plan_refs = [pr for pr in prs if pr.get("view_type") == "plan"]
        self.assertEqual(len(plan_refs), 1, "应有 plan 投影引用")
        pr = plan_refs[0]
        for key in ("sheet_id", "view_type", "component_id", "confidence", "geometry_origin"):
            self.assertIn(key, pr, f"projection_ref 缺少字段 {key}")

    def test_unresolved_projection_refs_recorded(self):
        from traceability.intake.tower_views import merge_view_bars
        m = _make_two_view_model(with_unresolved=True)
        merge_view_bars(m)

        df = m.components.get("drawing_file")
        self.assertIsNotNone(df, "应保留 drawing_file 组件")
        unresolved = df.properties.get("unresolved_projection_refs") or []
        self.assertGreaterEqual(len(unresolved), 1, "孤立投影应进入 unresolved_projection_refs")
        orphan = [u for u in unresolved if u.get("component_id") == "bar_plan_orphan"]
        self.assertEqual(len(orphan), 1, "孤儿投影 bar_plan_orphan 应被记录为 unresolved")


if __name__ == "__main__":
    unittest.main()
