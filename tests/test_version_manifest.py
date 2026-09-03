"""P0 版本固定回归测试：version.json 指纹 + sync 清理/SHA 校验。

覆盖：
    * collect_version_info / write_version_manifest：run_id 沿用 run_manifest、
      git_sha 40-hex、model_sha/skeleton_sha 与实测 sha256 一致、
      杆件/节点计数、参数化底段统计、A2 TP@500 摘要提取；
    * sync_assets：version.json 必拷、清单外旧文件被清理（prune）、
      拷贝后 SHA 一致、必需文件缺失显式失败。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.project.versioning import (  # noqa: E402
    collect_version_info, sha256_file, write_version_manifest,
)

sync_mod = None
try:
    spec = importlib.util.spec_from_file_location(
        "sync_demo_assets", REPO / "scripts/sync_demo_assets.py")
    assert spec and spec.loader
    sync_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync_mod)
except FileNotFoundError:
    pass


def _fixture_out(tmp: Path) -> Path:
    """最小交付目录：model.json + skeleton.glb + run_manifest + metrics。"""
    out = tmp / "deliver"
    out.mkdir()
    (out / "model.json").write_text(json.dumps({
        "components": {
            "n1": {"kind": "tower_node", "properties": {"z": 0.0}},
            "n2": {"kind": "tower_node", "properties": {"z": 6500.0}},
            "n3": {"kind": "tower_node", "properties": {"z": 36000.0}},
            "b1": {"kind": "tower_bar", "nodes": ["n1", "n2"],
                   "properties": {"geometry_class": "derived_parametric",
                                  "geometry_origin": "derived_parametric_base"}},
            "b2": {"kind": "tower_bar", "nodes": ["n2", "n3"],
                   "properties": {"geometry_class": "recognized",
                                  "geometry_origin": "dxf_geom"}},
        },
    }), encoding="utf-8")
    (out / "skeleton.glb").write_bytes(b"glb-fixture-bytes")
    (out / "run_manifest.json").write_text(
        json.dumps({"run_id": "deadbeef" * 8}), encoding="utf-8")
    (out / "metrics_multi_caliber.json").write_text(json.dumps({
        "calibers": {
            "pure": {"n_model": 10, "sweep": [
                {"tol": 200.0, "tp": 1}, {"tol": 500.0, "tp": 55, "precision": 0.2, "recall": 0.05}]},
            "full": {"n_model": 20, "sweep": [
                {"tol": 500.0, "tp": 221, "precision": 0.34, "recall": 0.21}]},
        },
        "effective": {"z_min_mm": 6500.0, "sweep": [
            {"tol": 500.0, "tp": 216, "precision": 0.34, "recall": 0.22}]},
    }), encoding="utf-8")
    return out


class TestVersionManifest(unittest.TestCase):
    def test_collect_version_info(self):
        with tempfile.TemporaryDirectory() as td:
            out = _fixture_out(Path(td))
            info = collect_version_info(out, REPO)
            self.assertEqual(info["run_id"], "deadbeef" * 8)
            self.assertEqual(info["model_sha"], sha256_file(out / "model.json"))
            self.assertEqual(info["skeleton_sha"], sha256_file(out / "skeleton.glb"))
            # git 仓库真实存在：sha 应为 40-hex（或极少数环境 None）
            self.assertTrue(info["git_sha"] is None or len(info["git_sha"]) == 40)
            self.assertIn("generated_at", info)
            self.assertEqual(info["model_components"], 2)
            self.assertEqual(info["model_nodes"], 3)
            self.assertEqual(info["z_range_mm"], [0.0, 36000.0])
            # 参数化底段：b1（z 0→6500）
            self.assertEqual(info["base_segment"]["bars"], 1)
            self.assertEqual(info["base_segment"]["z_range_mm"], [0.0, 6500.0])
            # A2 摘要：TP@500 提取（其它 tol 不混入）
            self.assertEqual(info["a2_front"]["pure"]["tp500"], 55)
            self.assertEqual(info["a2_front"]["full"]["tp500"], 221)
            self.assertEqual(info["a2_front"]["effective"]["z_min_mm"], 6500.0)
            self.assertNotIn("reconstructed", info["a2_front"])

    def test_write_version_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            out = _fixture_out(Path(td))
            info = write_version_manifest(out, REPO)
            on_disk = json.loads((out / "version.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["run_id"], info["run_id"])
            self.assertEqual(on_disk["model_sha"], info["model_sha"])
            self.assertIn("note", on_disk["base_segment"])

    def test_run_id_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            out = _fixture_out(Path(td))
            (out / "run_manifest.json").unlink()
            info = collect_version_info(out, REPO)
            self.assertIsInstance(info["run_id"], str)
            self.assertEqual(len(info["run_id"]), 32)


@unittest.skipIf(sync_mod is None, "sync_demo_assets 不可加载")
class TestSyncVersion(unittest.TestCase):
    def _src(self, tmp: Path) -> Path:
        src = _fixture_out(tmp)
        (src / "skeleton.bar_map.json").write_text(json.dumps([
            {"component_id": "b2", "role": "LEG", "geometry_origin": "dxf_geom"}]),
            encoding="utf-8")
        for name in ("metrics_by_role.json", "metrics_by_origin.json",
                     "evidence_report.json", "review_queue.json"):
            (src / name).write_text("{}", encoding="utf-8")
        write_version_manifest(src, REPO)
        return src

    def test_sync_copies_version_and_prunes_stale(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = self._src(tmp)
            dst = tmp / "web"
            dst.mkdir()
            (dst / "version.json").write_text('{"old": true}', encoding="utf-8")
            (dst / "history_artifact.glb").write_bytes(b"old-glb")
            (dst / "sub").mkdir()
            (dst / "sub/old.json").write_text("{}", encoding="utf-8")

            result = sync_mod.sync_assets(src, dst, prune=True)

            self.assertIn("version.json", result["copied"])
            self.assertIn("model.json", result["copied"])
            self.assertIn("bar_map.json", result["copied"])
            self.assertEqual(result["sha_mismatch"], [])
            # 旧文件被清理，新版本指纹就位
            self.assertFalse((dst / "history_artifact.glb").exists())
            self.assertFalse((dst / "sub/old.json").exists())
            v = json.loads((dst / "version.json").read_text(encoding="utf-8"))
            self.assertEqual(v["run_id"], "deadbeef" * 8)
            # 版本指纹与 dst 内实际文件哈希一致（P0.4 验收口径）
            self.assertEqual(v["model_sha"], sha256_file(dst / "model.json"))
            self.assertEqual(v["skeleton_sha"], sha256_file(dst / "skeleton.glb"))
            # bar_map 已并入 section（model.json 无 section → null）
            bm = json.loads((dst / "bar_map.json").read_text(encoding="utf-8"))
            self.assertEqual(bm[0]["section"], None)

    def test_sync_missing_required_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = self._src(tmp)
            (src / "version.json").unlink()   # 必需资产被移除 → 显式失败
            dst = tmp / "web"
            with self.assertRaises(FileNotFoundError):
                sync_mod.sync_assets(src, dst, prune=True)

    def test_sync_no_prune_keeps_stale(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            src = self._src(tmp)
            dst = tmp / "web"
            dst.mkdir()
            (dst / "history_artifact.glb").write_bytes(b"old-glb")
            result = sync_mod.sync_assets(src, dst, prune=False)
            self.assertEqual(result.get("pruned"), [])
            self.assertTrue((dst / "history_artifact.glb").exists())


if __name__ == "__main__":
    unittest.main()
