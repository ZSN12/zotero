"""P0 架构对齐（2026-09-05 审计）：observations / hypotheses 证据层测试。

对照审计要求：
  * observations：bar_id_evidence / DIM 样本 → 独立 component kind
    （observation），带稳定 ID + confidence；
  * hypotheses：diagonal_topology 候选 → kind=hypothesis，四态
    proposed/accepted/rejected/superseded，拒绝原因显式记录；
  * DAG：杆 → 证据观测边（改标注 → 杆 stale）；expand_4_face 不清除
    证据层组件。
"""
import unittest

from traceability.intake.evidence_layer import (
    depend_on_observations,
    hypothesis_census,
    label_observation_id,
    mark_hypotheses_accepted,
    observation_census,
    register_dim_observations,
    register_hypotheses,
    register_label_observations,
)
from traceability.model import Component, EngineeringModel, SourceRef, SourceType


def _model() -> EngineeringModel:
    m = EngineeringModel(name="t")
    m.add_component(Component(
        id="drawing_file", name="t", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "t.dxf", confidence=1.0),
        properties={},
    ))
    m.add_component(Component(
        id="n1", name="n1", kind="tower_node",
        source=SourceRef(SourceType.DRAWING, "t.dxf", confidence=1.0),
        properties={"x": 0.0, "y": 0.0, "z": 0.0},
    ))
    m.add_component(Component(
        id="n2", name="n2", kind="tower_node",
        source=SourceRef(SourceType.DRAWING, "t.dxf", confidence=1.0),
        properties={"x": 100.0, "y": 0.0, "z": 3000.0},
    ))
    m.add_component(Component(
        id="b1", name="b1", kind="tower_bar",
        source=SourceRef(SourceType.DRAWING, "t.dxf", confidence=0.9),
        properties={
            "from_node": "n1", "to_node": "n2",
            "bar_id_evidence": [{
                "sheet_id": "S1",
                "label_component_id": "text_1234",
                "text": "501", "association_method": "nearest",
                "distance": 42.0, "distance_unit": "drawing",
                "confidence": 0.85,
            }],
        },
    ))
    return m


class LabelObservationTest(unittest.TestCase):
    def test_register_and_depend(self):
        m = _model()
        made = register_label_observations(m, "S1", "t.dxf", {
            "1234": {"text": "501", "confidence": 0.85,
                     "association_method": "nearest",
                     "distance": 42.0, "distance_unit": "drawing",
                     "label_component_id": "text_1234"},
        })
        # 观测 ID 按文字实体（label_component_id）而非杆段 handle
        self.assertEqual(made, [label_observation_id("S1", "text_1234")])
        obs = m.components[made[0]]
        self.assertEqual(obs.kind, "observation")
        self.assertEqual(obs.properties["observation_kind"], "bar_label")
        self.assertAlmostEqual(obs.properties["confidence"], 0.85)
        # 杆 → 观测 DAG 边 + stale 传播
        depend_on_observations(m, "b1", made)
        self.assertIn(made[0], m.dependencies["b1"])
        stale = m.invalidate(set(made))
        self.assertIn("b1", stale, "改件号观测必须沿 DAG 传播到引用杆")

    def test_text_entity_dedup(self):
        """同一文字关联多杆（变体段）只建一个观测。"""
        m = _model()
        made = register_label_observations(m, "S1", "t.dxf", {
            "segA": {"text": "501", "confidence": 0.85,
                     "label_component_id": "text_99"},
            "segB": {"text": "501", "confidence": 0.85,
                     "label_component_id": "text_99"},
            "segC": {"text": "502", "confidence": 0.85,
                     "label_component_id": "text_88"},
        })
        self.assertEqual(len(made), 2)
        self.assertEqual(observation_census(m), {"bar_label": 2})

    def test_idempotent(self):
        m = _model()
        ev = {"1234": {"text": "501", "confidence": 0.85,
                       "label_component_id": "text_1234"}}
        register_label_observations(m, "S1", "t.dxf", ev)
        made2 = register_label_observations(m, "S1", "t.dxf", ev)
        self.assertEqual(made2, [], "同文字重复登记幂等跳过")
        self.assertEqual(observation_census(m), {"bar_label": 1})

    def test_depend_skips_missing(self):
        m = _model()
        n = depend_on_observations(m, "b1", ["obs_S1_label_nothandle"])
        self.assertEqual(n, 0)
        self.assertNotIn("b1", m.dependencies)


class DimObservationTest(unittest.TestCase):
    def test_dict_samples(self):
        m = _model()
        made = register_dim_observations(
            m, "S1", "t.dxf",
            [{"handle": "777", "value": 1600.0},
             {"handle": None, "value": 0.0}],   # 无 handle 跳过
            context="scale_calibration")
        self.assertEqual(made, ["obs_S1_dim_777"])
        obs = m.components["obs_S1_dim_777"]
        self.assertEqual(obs.properties["observation_kind"], "dim_sample")
        self.assertAlmostEqual(obs.properties["value"], 1600.0)
        self.assertEqual(observation_census(m), {"dim_sample": 1})


class HypothesisTest(unittest.TestCase):
    INTERP = {"kind": "fan", "z_lo": 12000.0, "z_hi": 16000.0,
              "score": 180.0, "n": 3, "evidence": ["bar_1", "bar_2"]}

    def test_four_states(self):
        m = _model()
        made = register_hypotheses(
            m, "S1", [self.INTERP],
            rejected=[{"kind": "fan", "z_lo": 13000.0, "z_hi": 17000.0,
                       "score": 3900.0, "reason": "span_off_grid"}],
            superseded=[{"kind": "original_projection", "z_lo": 0.0,
                         "z_hi": 0.0}])
        self.assertEqual(len(made), 3)
        self.assertEqual(hypothesis_census(m),
                         {"proposed": 1, "rejected": 1, "superseded": 1})
        # 拒绝原因显式落盘
        rej = m.components[made[1]]
        self.assertEqual(rej.properties["status"], "rejected")
        self.assertEqual(rej.properties["reject_reason"], "span_off_grid")
        # proposed → accepted
        n = mark_hypotheses_accepted(m, [made[0]])
        self.assertEqual(n, 1)
        self.assertEqual(
            m.components[made[0]].properties["status"], "accepted")
        self.assertEqual(hypothesis_census(m),
                         {"accepted": 1, "rejected": 1, "superseded": 1})

    def test_rejected_upgrades_inplace(self):
        """候选先登记 proposed 后被拒：原地改状态，不产生双组件。"""
        m = _model()
        interp = {"kind": "twist", "z_lo": 8000.0, "z_hi": 11000.0,
                  "score": 500.0, "n": 2, "evidence": []}
        register_hypotheses(m, "S1", [interp])
        hid = "hyp_S1_diagonal_topology_twist_8000_11000"
        register_hypotheses(
            m, "S1", [],
            rejected=[{"kind": "twist", "z_lo": 8000.0, "z_hi": 11000.0,
                       "score": 500.0, "reason": "duplicate_h"}])
        self.assertIn(hid, m.components)
        self.assertEqual(m.components[hid].properties["status"], "rejected")
        self.assertEqual(
            sum(1 for c in m.components.values() if c.kind == "hypothesis"), 1)

    def test_id_stability(self):
        """同一候选重复登记：ID 不漂移（分数漂移不换身份）。"""
        m = _model()
        register_hypotheses(m, "S1", [self.INTERP])
        made2 = register_hypotheses(m, "S1", [self.INTERP])
        self.assertEqual(made2, [])
        self.assertEqual(hypothesis_census(m), {"proposed": 1})


class ExpansionSurvivalTest(unittest.TestCase):
    def test_evidence_layer_survives_expand(self):
        """observation/hypothesis 组件在四面展开后保留（_KEEP_KINDS）。"""
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        m = _model()
        register_label_observations(m, "S1", "t.dxf",
                                    {"1234": {"text": "501", "confidence": 0.85,
                                              "label_component_id": "text_1234"}})
        register_hypotheses(m, "S1", [
            {"kind": "fan", "z_lo": 1000.0, "z_hi": 3000.0,
             "score": 100.0, "n": 2, "evidence": []}])
        # b1 需要可展开的视图属性
        m.components["b1"].properties.update({
            "view_type": "front", "drawing_view": "S1",
            "geometry_class": "recognized", "geometry_origin": "dxf_geom",
        })
        for n in ("n1", "n2"):
            m.components[n].properties.update({"view_type": "front",
                                               "drawing_view": "S1"})
        obs_id = label_observation_id("S1", "text_1234")
        depend_on_observations(m, "b1", [obs_id])
        expand_4_face_symmetry_model(m, add_diaphragms=False,
                                     weld_corner_legs=False)
        self.assertIn(obs_id, m.components,
                      "observation 组件必须跨四面展开存活")
        self.assertIn(
            "hyp_S1_diagonal_topology_fan_1000_3000", m.components,
            "hypothesis 组件必须跨四面展开存活")
        # 证据边重挂：展开后的杆变体（4f_b1_*）应仍指向观测
        front_id = next((cid for cid in m.components
                         if cid.startswith("4f_b1")), None)
        self.assertIsNotNone(front_id, "front 杆应被展开重建为 4f_b1_*")
        ups = m.dependencies.get(front_id) or set()
        self.assertIn(obs_id, ups,
                      "展开后杆 → 观测证据边必须按新 ID 重挂")


if __name__ == "__main__":
    unittest.main()
