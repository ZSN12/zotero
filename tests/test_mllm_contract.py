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
    _cache_content_meta,
    _cache_meta_matches,
    CACHE_META_KEY,
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


class CacheContentFingerprintTest(unittest.TestCase):
    """阶段2.3：视觉缓存内容指纹——旧缓存（缺 _cache_meta / crop 内容变化）必须失效。"""

    def _img(self, d: Path, name: str, content: bytes) -> str:
        p = d / name
        p.write_bytes(content)
        return str(p)

    def test_matching_meta_hits(self):
        with tempfile.TemporaryDirectory() as d:
            img = self._img(Path(d), "crop.png", b"png-bytes-v1")
            meta = _cache_content_meta(img, "prompt-A")
            parsed = {CACHE_META_KEY: meta, "bars": [{"x1": 1}]}
            self.assertTrue(_cache_meta_matches(parsed, img, "prompt-A"))

    def test_old_cache_without_meta_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            img = self._img(Path(d), "crop.png", b"png-bytes-v1")
            parsed = {"bars": [{"x1": 1}]}  # 旧式缓存，无 _cache_meta
            self.assertFalse(_cache_meta_matches(parsed, img, "prompt-A"))

    def test_crop_content_change_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            img = self._img(Path(d), "crop.png", b"png-bytes-v1")
            meta = _cache_content_meta(img, "prompt-A")
            parsed = {CACHE_META_KEY: meta, "bars": [{"x1": 1}]}
            # 同名文件内容变了（改 region/切片后重渲）→ crop_sha 不匹配
            img2 = self._img(Path(d), "crop.png", b"png-bytes-v2-changed")
            self.assertFalse(_cache_meta_matches(parsed, img2, "prompt-A"))

    def test_prompt_change_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            img = self._img(Path(d), "crop.png", b"png-bytes-v1")
            meta = _cache_content_meta(img, "prompt-A")
            parsed = {CACHE_META_KEY: meta, "bars": [{"x1": 1}]}
            self.assertFalse(_cache_meta_matches(parsed, img, "prompt-B-changed"))


class CenterlineClassifyParseTest(unittest.TestCase):
    """阶段2.4：候选中心线分类输出解析。"""

    def test_parse_keep_drop(self):
        from traceability.intake.mllm_tower_prompt import parse_centerline_classify_output
        keep, problems, warnings = parse_centerline_classify_output(
            {"keep": ["C001", "C003"], "drop": ["C002", "C004"]}
        )
        self.assertEqual(problems, [])
        self.assertEqual(keep, {"C001", "C003"})

    def test_parse_non_dict(self):
        from traceability.intake.mllm_tower_prompt import parse_centerline_classify_output
        keep, problems, _ = parse_centerline_classify_output(None)
        self.assertEqual(keep, set())
        self.assertTrue(problems)

    def test_keep_drop_conflict_keep_wins(self):
        from traceability.intake.mllm_tower_prompt import parse_centerline_classify_output
        keep, problems, warnings = parse_centerline_classify_output(
            {"keep": ["C001"], "drop": ["C001"]}
        )
        self.assertEqual(keep, {"C001"})
        self.assertEqual(problems, [])
        self.assertTrue(any("keep/drop" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
