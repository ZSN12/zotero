"""angle-tower 领域包门禁测试（P2-1 / 开源基座对标）。

覆盖两道硬门禁自身的正确性：
  * self_test 的三道子门（单测/冒烟/IR 完整性）——门禁会拦该拦的；
  * validate_public_ir 的五项检查——对构造的违规模型必须报 FAIL，
    对合规模型必须 PASS（防「门禁永远绿灯」的假阳性）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE_DIR = REPO / "domains" / "angle-tower" / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ValidatePublicIrTest(unittest.TestCase):
    """validate_public_ir：违规必须 FAIL，合规必须 PASS。"""

    @classmethod
    def setUpClass(cls):
        cls.vpi = _load("vpi_under_test", GATE_DIR / "validate_public_ir.py")
        cls.tmp = Path(__import__("tempfile").mkdtemp())
        # 合规基线模型（五项全过）
        cls.ok_model = {
            "name": "t",
            "components": {
                "b1": {
                    "id": "b1", "name": "b1", "kind": "tower_bar",
                    "source": {"source_type": "drawing", "reference": "s.dxf",
                               "confidence": 0.9},
                    "properties": {
                        "geometry_class": "recognized",
                        "geometry_origin": "dxf_geom",
                    },
                },
                "obs_s1_label_1": {
                    "id": "obs_s1_label_1", "name": "o", "kind": "observation",
                    "properties": {"observation_kind": "bar_label"},
                },
                "hyp_s1_dt_fan_1000_3000": {
                    "id": "hyp_s1_dt_fan_1000_3000", "name": "h",
                    "kind": "hypothesis",
                    "properties": {"status": "accepted"},
                },
            },
            "dimensions": {}, "connections": {}, "rules": {},
        }

    def _write(self, model: dict, version: dict | None = None) -> Path:
        p = self.tmp / f"m_{id(model) % 100000}.json"
        p.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
        if version is not None:
            (p.parent / (p.stem + "_v.json")).write_text(
                json.dumps(version, ensure_ascii=False), encoding="utf-8")
        return p

    def _run(self, model: dict, version: dict | None = None) -> int:
        import contextlib, io
        p = self._write(model, version)
        argv = [str(p)]
        if version is not None:
            argv += ["--version", str(p.parent / (p.stem + "_v.json"))]
        with contextlib.redirect_stdout(io.StringIO()):
            sys.argv = ["validate_public_ir.py"] + argv
            try:
                return self.vpi.main()
            except SystemExit as e:  # argparse 错误防御
                return int(e.code or 1)

    def test_compliant_model_passes(self):
        self.assertEqual(self._run(self.ok_model), 0)

    def test_missing_geometry_origin_fails(self):
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["b1"]["properties"].pop("geometry_origin")
        self.assertNotEqual(self._run(m), 0)

    def test_bad_observation_id_fails(self):
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["obs_s1_label_1"]["id"] = "random_id"
        m["components"]["random_id"] = m["components"].pop("obs_s1_label_1")
        self.assertNotEqual(self._run(m), 0)

    def test_bad_hypothesis_status_fails(self):
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["hyp_s1_dt_fan_1000_3000"]["properties"]["status"] = "maybe"
        self.assertNotEqual(self._run(m), 0)

    def test_bar_without_source_fails(self):
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["b1"].pop("source")
        self.assertNotEqual(self._run(m), 0)

    def test_undisclosed_gt_injection_fails(self):
        # 模型声明了注入键，但 version.json 无 gt_injected.surfaces → FAIL
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["drawing_file"] = {
            "id": "drawing_file", "name": "df", "kind": "drawing_file",
            "properties": {"terminal_pair_span_whitelist": [[0, 6500]]},
        }
        v = {"gt_injected": {}}
        self.assertNotEqual(self._run(m, v), 0)

    def test_disclosed_gt_injection_passes(self):
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["drawing_file"] = {
            "id": "drawing_file", "name": "df", "kind": "drawing_file",
            "properties": {"terminal_pair_span_whitelist": [[0, 6500]]},
        }
        v = {"gt_injected": {"surfaces": {
            "terminal_pair_span_whitelist": "1 pairs",
            "terminal_levels_injected": "override table",
        }}}
        self.assertEqual(self._run(m, v), 0)

    def test_merge_prefixed_observation_id_passes(self):
        # 跨册合并前缀 {stem}__obs_... 必须被接受为稳定 ID
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["35A1-JC1-02__obs_x_label_1"] = {
            "id": "35A1-JC1-02__obs_x_label_1", "name": "o",
            "kind": "observation",
            "properties": {"observation_kind": "bar_label"},
        }
        self.assertEqual(self._run(m), 0)


class SelfTestContractTest(unittest.TestCase):
    """self_test 门禁自身的结构契约。"""

    def test_gates_exist_and_executable(self):
        st = GATE_DIR / "self_test.py"
        vpi = GATE_DIR / "validate_public_ir.py"
        self.assertTrue(st.exists() and vpi.exists())
        for g in (st, vpi):
            self.assertIn("#!", g.read_text(encoding="utf-8")[:5])


if __name__ == "__main__":
    unittest.main()
