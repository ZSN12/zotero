"""Phase 6.4 回归测试：bar_map 的 section 字段（TASK_VIEWER_POLISH 合入门槛）。

覆盖：
    * merge_section_into_bar_map 单元：合法格式保留 / 污染值与缺失归 null /
      无关联条目（review_*）归 null
    * sync_assets 集成：同步后 bar_map 每条记录都有 section 键，
      非 null 值全部匹配任务书钉死的格式（L\\d+X\\d+ 或 Q\\d+L\\d+X\\d+）
    * 结构异常回退：bar_map 非 list → 原样拷贝 + warning，不炸同步
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync_mod = _load_script("sync_demo_assets")

# 任务书钉死的合法截面格式
VALID_RE = re.compile(r"^(?:Q\d+)?L\d+(?:\.\d+)?X\d+(?:\.\d+)?$")


def _mk_model_with_sections():
    def bar(cid, section):
        props = {"from_node": "N1", "to_node": "N2", "face": "f",
                 "geometry_class": "recognized", "geometry_origin": "dxf_geom",
                 "role": "LEG"}
        if section is not _MISSING:
            props["section"] = section
        return {"id": cid, "kind": "tower_bar", "name": cid, "properties": props}

    _MISSING = object()
    return {"name": "t", "components": {
        "b_ok1": bar("b_ok1", "L40X3"),
        "b_ok2": bar("b_ok2", "Q345L100X7"),
        "b_bolt": bar("b_bolt", "5M16X40"),      # 螺栓规格污染 → null
        "b_plate": bar("b_plate", "-6X146"),     # 钢板规格 → null
        "b_empty": bar("b_empty", ""),           # 空串 → null
        "b_none": bar("b_none", None),           # 显式 null → null
        "b_missing": {"id": "b_missing", "kind": "tower_bar", "name": "b_missing",
                      "properties": {"from_node": "N1", "to_node": "N2"}},  # 无字段 → null
    }}


class TestMergeSection(unittest.TestCase):
    def test_valid_kept_invalid_null(self):
        model = _mk_model_with_sections()
        bar_map = [{"component_id": cid, "role": "LEG", "geometry_origin": "dxf_geom"}
                   for cid in model["components"]]
        merged = sync_mod.merge_section_into_bar_map(bar_map, model)
        by_id = {e["component_id"]: e for e in merged}
        self.assertEqual(by_id["b_ok1"]["section"], "L40X3")
        self.assertEqual(by_id["b_ok2"]["section"], "Q345L100X7")
        for cid in ("b_bolt", "b_plate", "b_empty", "b_none", "b_missing"):
            self.assertIsNone(by_id[cid]["section"], cid)

    def test_unrelated_entry_gets_null_section(self):
        """bar_map 里 model 无对应的条目（如 review_*）也要有 section 键（null）。"""
        merged = sync_mod.merge_section_into_bar_map(
            [{"component_id": "review_N1", "role": None}], {"components": {}})
        self.assertIn("section", merged[0])
        self.assertIsNone(merged[0]["section"])

    def test_input_not_mutated(self):
        entry = {"component_id": "x"}
        sync_mod.merge_section_into_bar_map([entry], {"components": {}})
        self.assertNotIn("section", entry)  # 浅拷贝，不污染原记录


class TestSyncIntegration(unittest.TestCase):
    def _make_src(self, tmp: Path) -> Path:
        src = tmp / "src"
        src.mkdir()
        model = _mk_model_with_sections()
        bar_map = [{"component_id": cid, "role": "LEG", "geometry_origin": "dxf_geom"}
                   for cid in model["components"]]
        for src_name, _, _ in sync_mod.ASSET_MANIFEST:
            f = src / src_name
            f.parent.mkdir(parents=True, exist_ok=True)
            if src_name == "skeleton.bar_map.json":
                f.write_text(json.dumps(bar_map), encoding="utf-8")
            elif src_name == "model.json":
                f.write_text(json.dumps(model), encoding="utf-8")
            else:
                f.write_text("{}", encoding="utf-8")
        return src

    def test_synced_bar_map_all_have_valid_section(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result = sync_mod.sync_assets(self._make_src(tmp), tmp / "dst")
            self.assertEqual(len(result["copied"]), len(sync_mod.ASSET_MANIFEST))
            self.assertEqual(result["warnings"], [])
            merged = json.loads((tmp / "dst" / "bar_map.json").read_text(encoding="utf-8"))
            self.assertEqual(len(merged), 7)
            for e in merged:
                self.assertIn("section", e)
                if e["section"] is not None:
                    self.assertRegex(e["section"], VALID_RE)

    def test_malformed_bar_map_falls_back_with_warning(self):
        """bar_map 结构异常 → 原样拷贝 + warning，同步整体不失败。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = self._make_src(tmp)
            (src / "skeleton.bar_map.json").write_text('{"not": "a list"}', encoding="utf-8")
            result = sync_mod.sync_assets(src, tmp / "dst")
            self.assertEqual(len(result["copied"]), len(sync_mod.ASSET_MANIFEST))
            self.assertTrue(any("section 合并失败" in w for w in result["warnings"]))
            raw = json.loads((tmp / "dst" / "bar_map.json").read_text(encoding="utf-8"))
            self.assertEqual(raw, {"not": "a list"})


if __name__ == "__main__":
    unittest.main()
