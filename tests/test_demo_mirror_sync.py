# -*- coding: utf-8 -*-
"""镜像一致性检查（check_demo_mirror_sync）单元测试——CI 通道。

背景（2026-09-05 审计缺口）：web/demo/<塔>/latest_deliver 是用户实际
看到的交付，out/ 是内部交付；out/ 与 web/demo/** 均 gitignore，CI
无法直接比真实产物。故本测试用合成夹具验证检查逻辑本身，跑在
pytest 快层（ci.yml "tests" job）——镜像过期/跨塔指纹污染两类事故
（220 停更、ZC1 镜像带 JC1 的 overlay_sha）都必须被抓住。
"""
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "check_demo_mirror_sync.py"
_spec = importlib.util.spec_from_file_location("check_demo_mirror_sync", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_demo_mirror_sync"] = _mod
_spec.loader.exec_module(_mod)

REQUIRED = _mod.REQUIRED_FILES
OPTIONAL = _mod.OPTIONAL_FILES


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make_tower(root: Path, name: str, overlay_body: bytes,
                mirror_a2_overlay_sha: str | None = None,
                stale_file: str | None = None,
                drop_file: str | None = None):
    """造一个 (src, dst, overlay) 三元组夹具。默认全同步、全通过。"""
    src = root / f"out-{name}"
    dst = root / f"demo-{name}"
    ov = root / f"overlay-{name}.json"
    ov.write_bytes(overlay_body)
    ov_sha = _sha_bytes(overlay_body)

    # version.json 声明 overlay_path（指纹链入口）
    _write(src / "version.json", {"overlay_path": str(ov), "overlay_sha": ov_sha})
    # a2_dual_view.json 的 eval_binding.overlay_sha256（镜像侧）
    eb = {"overlay_sha256": mirror_a2_overlay_sha if mirror_a2_overlay_sha is not None else ov_sha}
    _write(src / "a2_dual_view.json", {"eval_binding": eb, "profiles": {}})

    bar_map_src = [{"component_id": "b1"}]
    model = {"components": {"b1": {"kind": "tower_bar",
                                   "properties": {"section": "L40X3"}}}}
    _write(src / "skeleton.bar_map.json", bar_map_src)
    _write(src / "model.json", model)
    for sfn, _dfn in REQUIRED:
        if sfn in ("version.json", "skeleton.bar_map.json", "model.json"):
            continue
        _write(src / sfn, {"stub": sfn})

    # 镜像：按 sync_demo_assets 语义生成（bar_map 走变换）
    for sfn, dfn in REQUIRED + OPTIONAL:
        s = src / sfn
        if not s.exists():
            continue
        if dfn == drop_file:
            continue
        if sfn == "skeleton.bar_map.json":
            merged = _mod_merge(bar_map_src, model)
            (dst / dfn).parent.mkdir(parents=True, exist_ok=True)
            (dst / dfn).write_text(
                json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            (dst / dfn).parent.mkdir(parents=True, exist_ok=True)
            (dst / dfn).write_bytes(s.read_bytes())

    if stale_file:
        p = dst / stale_file
        p.write_bytes((p.read_bytes() + b"STALE") if p.suffix != ".json"
                      else json.dumps({"stale": True}).encode("utf-8"))
    return src, dst, ov, ov_sha


def _mod_merge(bar_map, model):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import sync_demo_assets
    return sync_demo_assets.merge_section_into_bar_map(bar_map, model)


class MirrorSyncCheckTest(unittest.TestCase):
    def _tmp(self):
        import tempfile
        return tempfile.TemporaryDirectory()

    def test_fresh_mirror_passes(self):
        """全同步镜像 → 零失败。"""
        with self._tmp() as td:
            root = Path(td)
            src, dst, _ov, _sha = _make_tower(root, "T1", b'{"a":1}')
            fails, notes = _mod.check_tower("T1", src, dst)
            self.assertEqual(fails, [])
            self.assertEqual(notes, [])

    def test_stale_required_file_fails(self):
        """镜像里某必需文件过期（out 重跑后未再同步）→ 必须抓住。"""
        with self._tmp() as td:
            root = Path(td)
            src, dst, _ov, _sha = _make_tower(
                root, "T1", b'{"a":1}', stale_file="metrics_by_role.json")
            fails, _ = _mod.check_tower("T1", src, dst)
            self.assertTrue(any("metrics_by_role.json" in f for f in fails))

    def test_stale_bar_map_fails(self):
        """bar_map.json 过期（变换后内容不一致）→ 抓住（变换感知比对）。"""
        with self._tmp() as td:
            root = Path(td)
            src, dst, _ov, _sha = _make_tower(
                root, "T1", b'{"a":1}', stale_file="bar_map.json")
            fails, _ = _mod.check_tower("T1", src, dst)
            self.assertTrue(any("bar_map.json" in f for f in fails))

    def test_missing_required_file_fails(self):
        """镜像缺必需文件 → 抓住。"""
        with self._tmp() as td:
            root = Path(td)
            src, dst, _ov, _sha = _make_tower(
                root, "T1", b'{"a":1}', drop_file="evidence_report.json")
            fails, _ = _mod.check_tower("T1", src, dst)
            self.assertTrue(any("evidence_report.json" in f for f in fails))

    def test_cross_tower_overlay_contamination_fails(self):
        """跨塔指纹污染（镜像 a2 带别塔 overlay sha）→ 抓住（C 检查）。"""
        with self._tmp() as td:
            root = Path(td)
            # 塔 A 正常；塔 B 的镜像里 a2 指纹错写成 A 的 overlay sha
            srcA, dstA, _ovA, shaA = _make_tower(root, "A", b'{"A":1}')
            srcB, dstB, _ovB, shaB = _make_tower(
                root, "B", b'{"B":2}', mirror_a2_overlay_sha=shaA)
            self.assertNotEqual(shaA, shaB)
            failsB, _ = _mod.check_tower("B", srcB, dstB)
            self.assertTrue(any("overlay" in f for f in failsB),
                            f"应抓到跨塔污染: {failsB}")
            # A 不受影响
            failsA, _ = _mod.check_tower("A", srcA, dstA)
            self.assertEqual(failsA, [])

    def test_src_missing_is_skip_not_fail(self):
        """src 不存在 → 跳过（note），不失败（CI 无产物场景友好）。"""
        with self._tmp() as td:
            root = Path(td)
            fails, notes = _mod.check_tower(
                "T1", root / "out-none", root / "demo-none")
            self.assertEqual(fails, [])
            self.assertEqual(len(notes), 1)

    def test_version_json_drift_fails(self):
        """镜像 version.json 与 src 不一致（run_id/git_sha 漂移）→ 抓住。"""
        with self._tmp() as td:
            root = Path(td)
            src, dst, _ov, _sha = _make_tower(root, "T1", b'{"a":1}')
            v = json.loads((dst / "version.json").read_text())
            v["git_sha"] = "deadbeef"
            (dst / "version.json").write_text(
                json.dumps(v, ensure_ascii=False), encoding="utf-8")
            fails, _ = _mod.check_tower("T1", src, dst)
            self.assertTrue(any("version.json" in f for f in fails))


if __name__ == "__main__":
    unittest.main()
