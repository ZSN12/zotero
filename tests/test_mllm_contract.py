"""测试 MLLM 后端选择 + Skill 输出契约。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from traceability.intake.mllm_backend import (
    CandidateObject,
    DrawingInput,
    ModelCandidate,
    MLLMBackend,
    NullBackend,
    RuleBasedBackend,
    choose_backend,
)
from traceability.skill.contract import to_engineering_model
from traceability.model import DimensionOrigin


class BackendTest(unittest.TestCase):
    def test_choose_rule_backend_for_dxf(self):
        backend = choose_backend(DrawingInput(path="t.dxf", kind="dxf"))
        self.assertIsInstance(backend, RuleBasedBackend)

    def test_choose_null_when_no_api_for_scan(self):
        # 未配置 API -> 扫描图走 NullBackend 兜底
        # 显式传空 key（api_key="" 表示禁用），隔离宿主环境变量干扰
        backend = choose_backend(
            DrawingInput(path="t.png", kind="scan"),
            mllm=MLLMBackend(api_key=""),
        )
        self.assertIsInstance(backend, NullBackend)

    def test_null_backend_returns_placeholder(self):
        cand = NullBackend().analyze(DrawingInput(path="t.png", kind="scan"))
        self.assertEqual(len(cand.objects), 1)
        self.assertEqual(cand.objects[0].data["origin"], "placeholder")

    def test_rule_backend_on_dxf(self):
        from traceability.intake.tower_demo_dxf import make_demo_tower_dxf
        with tempfile.TemporaryDirectory() as d:
            dxf = make_demo_tower_dxf(Path(d) / "t.dxf")
            cand = RuleBasedBackend().analyze(DrawingInput(path=dxf, kind="dxf"))
        self.assertGreater(len(cand.objects), 0)
        self.assertTrue(any(o.obj_type == "component" for o in cand.objects))


class ContractTest(unittest.TestCase):
    def test_candidate_to_model(self):
        cand = ModelCandidate(
            input=DrawingInput(path="t.png", kind="scan"),
            objects=[
                CandidateObject(obj_type="component", data={
                    "id": "c1", "kind": "tower_bar", "name": "杆件 G01",
                    "properties": {"bar_id": "G01", "section": "L100x8"},
                }, confidence=0.8),
                CandidateObject(obj_type="dimension", data={
                    "id": "d1", "name": "长度", "value": None, "unit": "mm",
                    "origin": "measured",
                }, confidence=0.9),
            ],
        )
        model = to_engineering_model(cand)
        self.assertIn("c1", model.components)
        # 模型识别置信度封顶 0.9
        self.assertLessEqual(model.components["c1"].source.confidence, 0.9)
        # value=None 强制 placeholder
        self.assertEqual(model.dimensions["d1"].origin, DimensionOrigin.PLACEHOLDER)

    def test_no_source_gets_unknown(self):
        cand = ModelCandidate(
            input=DrawingInput(path="t.png", kind="scan"),
            objects=[CandidateObject(obj_type="component", data={
                "id": "c1", "kind": "tower_bar", "name": "x",
            }, source=None)],
        )
        model = to_engineering_model(cand)
        self.assertEqual(model.components["c1"].source.confidence, 0.0)
        self.assertEqual(model.components["c1"].source.source_type.value, "unknown")


if __name__ == "__main__":
    unittest.main()
