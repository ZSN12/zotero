"""Phase 6 回归测试：diff.glb 生成器 + demo 资产同步（TASK_VIEWER_3D 任务 C）。

覆盖：
    * 三类 diff 计数（1 不变 + 1 新增 + 1 删除，derived 排除）与 diff_report 格式
    * GLB 生成成功且 mesh 节点名 = component_id（viewer 按名过滤的前提）
    * tol 边界（30mm 位移：tol=50 判未变 / tol=20 判删+增）
    * 同模型 diff 诚实输出 0/0/N
    * sync_demo_assets 清单完整性、必需缺失报错、可选缺失降级
"""
from __future__ import annotations

import importlib.util
import json
import struct
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


diff_mod = _load_script("generate_diff_glb")
sync_mod = _load_script("sync_demo_assets")


def _mkmodel(bars: dict, nodes: dict) -> dict:
    """构造最小 model.json 结构。bars: cid → (from, to, face, geometry_class)。"""
    comps = {}
    for nid, xyz in nodes.items():
        comps[nid] = {"id": nid, "kind": "tower_node", "name": nid,
                      "properties": {"x": xyz[0], "y": xyz[1], "z": xyz[2]}}
    for cid, (f, t, face, gc) in bars.items():
        comps[cid] = {"id": cid, "kind": "tower_bar", "name": cid,
                      "properties": {"from_node": f, "to_node": t, "face": face,
                                     "geometry_class": gc,
                                     "geometry_origin": "dxf_geom", "role": "LEG"}}
    return {"name": "fixture", "version": "1", "components": comps,
            "dimensions": {}, "connections": {}, "rules": {},
            "dependencies": {}, "staleness": {}}


NODES = {"N1": (0, 0, 0), "N2": (0, 0, 5000), "N3": (1000, 0, 0),
         "N4": (1000, 0, 5000), "N5": (2000, 0, 0)}
# 旧模型：bA 保留（新模型里端点移 30mm）、bB 删除、bD 横隔不变、bX derived 应排除
OLD_BARS = {"bA": ("N1", "N2", "f", "recognized"),
            "bB": ("N3", "N4", "f", "recognized"),
            "bD": ("N1", "N3", "diaphragm", "recognized"),
            "bX": ("N1", "N5", "f", "derived")}
# 新模型：bA 未变（微调）、bD 不变、bC 新增
NEW_BARS = {"bA": ("N1", "N2", "f", "recognized"),
            "bD": ("N1", "N3", "diaphragm", "recognized"),
            "bC": ("N3", "N5", "f", "recognized")}


def _write_pair(tmp: Path, move_z_mm: float = 30.0) -> tuple:
    old = _mkmodel(OLD_BARS, NODES)
    new_nodes = dict(NODES)
    new_nodes["N2"] = (0, 0, 5000 + move_z_mm)
    new = _mkmodel(NEW_BARS, new_nodes)
    old_p, new_p = tmp / "old.json", tmp / "new.json"
    old_p.write_text(json.dumps(old), encoding="utf-8")
    new_p.write_text(json.dumps(new), encoding="utf-8")
    return old_p, new_p


def _glb_node_names(glb_path: Path) -> list:
    data = glb_path.read_bytes()
    assert data[:4] == b"glTF", "不是 GLB 二进制"
    jlen = struct.unpack("<I", data[12:16])[0]
    j = json.loads(data[20:20 + jlen])
    return [n.get("name", "") for n in j.get("nodes", [])]


class TestGenerateDiffGlb(unittest.TestCase):
    def test_three_way_counts_and_report_format(self):
        """1 不变 + 1 新增 + 1 删除（+1 derived 排除）三类计数正确。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            old_p, new_p = _write_pair(tmp)
            rep = diff_mod.generate_diff(old_p, new_p, 50.0, tmp)
            self.assertEqual(rep["summary"], {"added": 1, "removed": 1, "unchanged": 2})
            self.assertEqual(rep["added"], ["bC"])
            self.assertEqual(rep["removed"], ["bB"])
            self.assertEqual(rep["unchanged_count"], 2)
            # derived 排除：旧侧只数 bA/bB/bD 三根
            self.assertEqual(rep["n_old_bars"], 3)
            self.assertEqual(rep["n_new_bars"], 3)
            # 任务书钉死的报告键
            on_disk = json.loads((tmp / "diff_report.json").read_text(encoding="utf-8"))
            for key in ("added", "removed", "unchanged_count", "summary"):
                self.assertIn(key, on_disk)
            for key in ("added", "removed", "unchanged"):
                self.assertIn(key, on_disk["summary"])

    def test_glb_generated_with_component_id_names(self):
        """GLB 生成成功，mesh 节点名 = component_id（viewer 过滤前提）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            old_p, new_p = _write_pair(tmp)
            diff_mod.generate_diff(old_p, new_p, 50.0, tmp)
            glb = tmp / "diff.glb"
            self.assertTrue(glb.exists())
            self.assertGreater(glb.stat().st_size, 1000)
            names = _glb_node_names(glb)
            self.assertEqual(len(names), 4)  # bA bD 未变 + bC 新增 + bB 删除
            self.assertEqual(set(names), {"bA", "bD", "bC", "bB"})

    def test_tol_boundary(self):
        """30mm 位移：tol=50 判未变；tol=20 翻转为删+增。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            old_p, new_p = _write_pair(tmp, move_z_mm=30.0)
            rep50 = diff_mod.generate_diff(old_p, new_p, 50.0, tmp / "t50")
            self.assertEqual(rep50["summary"], {"added": 1, "removed": 1, "unchanged": 2})
            self.assertEqual(rep50["moved_within_tol"], [["bA", "bA", 30.0]])
            rep20 = diff_mod.generate_diff(old_p, new_p, 20.0, tmp / "t20")
            self.assertEqual(rep20["summary"], {"added": 2, "removed": 2, "unchanged": 1})

    def test_identical_models_honest_empty_diff(self):
        """同模型 diff：0 新增 / 0 删除 / 全未变（诚实输出，不造假差异）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            old_p, _ = _write_pair(tmp)
            rep = diff_mod.generate_diff(old_p, old_p, 50.0, tmp)
            self.assertEqual(rep["summary"]["added"], 0)
            self.assertEqual(rep["summary"]["removed"], 0)
            self.assertEqual(rep["summary"]["unchanged"], rep["n_old_bars"])

    def test_side_swap_matching(self):
        """端点反接（from/to 互换）仍判同一根杆。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            old = _mkmodel({"bS": ("N1", "N2", "f", "recognized")}, NODES)
            new = _mkmodel({"bS2": ("N2", "N1", "f", "recognized")}, NODES)
            (tmp / "o.json").write_text(json.dumps(old), encoding="utf-8")
            (tmp / "n.json").write_text(json.dumps(new), encoding="utf-8")
            rep = diff_mod.generate_diff(tmp / "o.json", tmp / "n.json", 50.0, tmp)
            self.assertEqual(rep["summary"], {"added": 0, "removed": 0, "unchanged": 1})


class TestSyncDemoAssets(unittest.TestCase):
    def _make_src(self, tmp: Path, skip: set = ()) -> Path:
        src = tmp / "src"
        src.mkdir()
        for src_name, _, _ in sync_mod.ASSET_MANIFEST:
            if src_name in skip:
                continue
            (src / src_name).write_text(f'{{"fixture": "{src_name}"}}', encoding="utf-8")
        return src

    def test_manifest_complete_copy(self):
        """全清单同步：10 个文件全部落位，bar_map 按目标名重命名。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = self._make_src(tmp)
            result = sync_mod.sync_assets(src, tmp / "dst")
            self.assertEqual(len(result["copied"]), 10)
            self.assertEqual(result["skipped_optional"], [])
            for _, dst_name, _ in sync_mod.ASSET_MANIFEST:
                self.assertTrue((tmp / "dst" / dst_name).exists(), dst_name)
            self.assertEqual(
                json.loads((tmp / "dst" / "bar_map.json").read_text(encoding="utf-8")),
                {"fixture": "skeleton.bar_map.json"})

    def test_missing_required_raises(self):
        """必需资产缺失 → FileNotFoundError（不允许静默出残缺 demo）。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = self._make_src(tmp, skip={"skeleton.glb"})
            with self.assertRaises(FileNotFoundError):
                sync_mod.sync_assets(src, tmp / "dst")

    def test_missing_diff_is_optional(self):
        """diff 两件缺失 → 警告降级（skipped），viewer 其余模式仍可用。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = self._make_src(tmp, skip={"diff.glb", "diff_report.json"})
            result = sync_mod.sync_assets(src, tmp / "dst")
            self.assertEqual(len(result["copied"]), 8)
            self.assertEqual(set(result["skipped_optional"]), {"diff.glb", "diff_report.json"})


if __name__ == "__main__":
    unittest.main()
