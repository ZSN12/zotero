"""angle-tower 领域包开源基座入口测试（init/validate/run_layer）。

2026-09-03 对标增量：六层契约从文档变成可执行入口——
  * init_domain.py 脚手架工作区（overlay 模板带纪律说明）；
  * validate_workspace.py 跑批前把关配置（z-only 注入面 fail-closed、
    BOM member 行、册-区域一致性、GT caveats）；
  * run_layer.py 每层独立可跑/可审计（canonical 产物，不重演编排）。

测试策略（防「结构契约测不到行为」）：
  * 全链路：110kv 内置示例走 init → validate → L1..L6 六层全过；
  * 负向：未知 GT 注入面必须 FAIL；BOM 无 member 行必须 FAIL；
    view_regions 幽灵册必须 FAIL；GT 无 caveats 必须 FAIL；
  * 审计模式：真实 JC1 交付产物上 L1-L4/L6 过、L5 诚实报 pending。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "domains" / "angle-tower" / "scripts"
EXAMPLES = REPO / "examples"

JC1_OUT = REPO / "out" / "35A1-JC1-full-deliver"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class InitDomainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = self.tmp / "tower-x"
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "init_domain.py"),
             str(self.ws), "--name", "tower-x", "--gt"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_scaffold_structure(self):
        for rel in ("overlay.json", "dxf", "bom/bom.csv",
                    "gt/ground_truth.json", "README.md", "out/.gitignore"):
            self.assertTrue((self.ws / rel).exists(), rel)
        ov = json.loads((self.ws / "overlay.json").read_text(encoding="utf-8"))
        self.assertEqual(ov["name"], "tower-x")
        # 模板必须自带纪律说明（铁律 1 落在配置里）
        self.assertTrue(any("x/y" in str(d) for d in ov["_doc"]))
        # GT 注入键默认只给 z-only 形态
        for k in ov:
            if k.startswith("gt_") or k.startswith("use_gt_"):
                self.assertIn(k, {
                    "gt_platform_levels_override",
                    "gt_terminal_levels_override",
                    "gt_diaphragm_levels_override"})

    def test_refuses_nonempty_dir(self):
        (self.ws / "sentinel").write_text("x", encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "init_domain.py"),
             str(self.ws), "--name", "tower-x"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("非空", r.stderr)


class ValidateWorkspaceTest(unittest.TestCase):
    """validate_workspace：该拦的拦，该过的过。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = self.tmp / "w"
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "init_domain.py"),
             str(self.ws), "--name", "w"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)
        # 单册干净工作区：删掉 view_regions 占位
        ov_p = self.ws / "overlay.json"
        ov = json.loads(ov_p.read_text(encoding="utf-8"))
        for k in ("view_regions", "_doc_view_regions",
                  "cross_file_views", "diagonal_topology_sheets"):
            ov.pop(k, None)
        ov_p.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
        shutil.copy(EXAMPLES / "tower_110kv.dxf", self.ws / "dxf")
        shutil.copy(EXAMPLES / "tower_110kv_bom.csv", self.ws / "bom" / "bom.csv")

    def _validate(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_workspace.py"), str(self.ws)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO))

    def test_clean_single_sheet_passes(self):
        r = self._validate()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unknown_gt_surface_fails_closed(self):
        ov_p = self.ws / "overlay.json"
        ov = json.loads(ov_p.read_text(encoding="utf-8"))
        ov["gt_bar_xy_coordinates"] = [[0, 0], [100, 200]]  # 未登记 + x/y
        ov_p.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
        r = self._validate()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("未登记的 GT 注入面", r.stdout)

    def test_bom_without_member_rows_fails(self):
        (self.ws / "bom" / "bom.csv").write_text(
            "bar_id,section,length_mm,qty\n316,5M16X40,60,4\n", encoding="utf-8")
        r = self._validate()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("member", r.stdout)

    def test_ghost_sheet_in_view_regions_fails(self):
        ov_p = self.ws / "overlay.json"
        ov = json.loads(ov_p.read_text(encoding="utf-8"))
        ov["view_regions"] = {"no-such-sheet": {"z_lo": 0, "z_hi": 1}}
        ov_p.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
        r = self._validate()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no-such-sheet", r.stdout)

    def test_gt_without_caveats_fails(self):
        gt_dir = self.ws / "gt"
        gt_dir.mkdir()
        (gt_dir / "ground_truth.json").write_text(
            json.dumps({"source": "glb_reextract", "bars": []}),
            encoding="utf-8")
        r = self._validate()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("caveats", r.stdout)


class RunLayerTest(unittest.TestCase):
    """run_layer：110kv 内置示例六层全过 + JC1 真实交付审计。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.ws = cls.tmp / "t110"
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "init_domain.py"),
             str(cls.ws), "--name", "probe-110kv"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        assert r.returncode == 0, r.stderr
        ov_p = cls.ws / "overlay.json"
        ov = json.loads(ov_p.read_text(encoding="utf-8"))
        for k in ("view_regions", "_doc_view_regions",
                  "cross_file_views", "diagonal_topology_sheets"):
            ov.pop(k, None)
        ov_p.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
        shutil.copy(EXAMPLES / "tower_110kv.dxf", cls.ws / "dxf")
        shutil.copy(EXAMPLES / "tower_110kv_bom.csv", cls.ws / "bom" / "bom.csv")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_six_layer_chain_on_110kv(self):
        outs = {}
        for layer in ("1", "2", "3", "4", "5", "6"):
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "run_layer.py"), layer,
                 "--workspace", str(self.ws)],
                capture_output=True, text=True, timeout=600, cwd=str(REPO))
            outs[layer] = r
            self.assertEqual(
                r.returncode, 0,
                f"L{layer} 应 PASS：\n{r.stdout[-600:]}\n{r.stderr[-300:]}")
        # 首层跑管线，后续复用
        src1 = json.loads((self.ws / "out" / "layer1_drawing_audit.json")
                          .read_text(encoding="utf-8"))["_layer"]["model_source"]
        src2 = json.loads((self.ws / "out" / "layer2_hypothesis_audit.json")
                          .read_text(encoding="utf-8"))["_layer"]["model_source"]
        self.assertIn("run_tower", src1)
        self.assertIn("复用", src2)
        # 六份审计报告齐备且非空
        names = {"1": "drawing", "2": "hypothesis", "3": "rebuild",
                 "4": "semantic-ir", "5": "validation-gate", "6": "complete-tower"}
        for n, nm in names.items():
            p = self.ws / "out" / f"layer{n}_{nm}_audit.json"
            self.assertTrue(p.exists(), p)

    @unittest.skipUnless(
        (JC1_OUT / "model.json").exists(),
        "JC1 交付产物不存在（先跑 scripts/run_35A1_jc1_full.py）")
    def test_audit_only_on_jc1_delivery(self):
        """审计模式吃 canonical 产物：L1-L4/L6 过；L5 诚实报 pending。"""
        for layer in ("1", "2", "3", "4", "6"):
            r = subprocess.run(
                [sys.executable, str(SCRIPTS / "run_layer.py"), layer,
                 "--out-dir", str(JC1_OUT)],
                capture_output=True, text=True, timeout=120, cwd=str(REPO))
            self.assertEqual(r.returncode, 0,
                             f"JC1 L{layer}：\n{r.stdout[-400:]}")
        r5 = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_layer.py"), "5",
             "--out-dir", str(JC1_OUT)],
            capture_output=True, text=True, timeout=120, cwd=str(REPO))
        self.assertEqual(r5.returncode, 1)  # 3 条 pending = 诚实复核态
        self.assertIn("r_bom_length_match", r5.stdout)

    def test_layer_without_artifacts_or_workspace_fails(self):
        empty = self.tmp / "empty-out"
        empty.mkdir()
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_layer.py"), "1",
             "--out-dir", str(empty)],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
