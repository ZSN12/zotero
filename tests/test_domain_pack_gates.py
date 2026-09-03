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
        # 干净 overlay（无 GT 注入键）——Bug C 后 GT 披露以
        # version.json.overlay_path + overlay 文件闭环，合规基线必须有这对。
        cls.ok_overlay = cls.tmp / "clean_overlay.json"
        cls.ok_overlay.write_text(json.dumps({"name": "clean"}), encoding="utf-8")
        # 带 GT 注入声明的 overlay（回归/正向两用）
        cls.gt_overlay = cls.tmp / "gt_overlay.json"
        cls.gt_overlay.write_text(json.dumps({
            "terminal_pair_span_whitelist": [[0, 6500]],
            "terminal_pair_structure": {"enabled": True},
        }), encoding="utf-8")
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
        p = self.tmp / f"m_{id(model) % 100000}_{len(str(version))}.json"
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

    def _clean_version(self) -> dict:
        return {"overlay_path": str(self.ok_overlay)}

    def _gt_version(self) -> dict:
        return {"overlay_path": str(self.gt_overlay)}

    def test_compliant_model_passes(self):
        self.assertEqual(self._run(self.ok_model, self._clean_version()), 0)

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
        """Bug C 重写：overlay 声明注入键（whitelist + terminal_pair_structure）
        但 version.json gt_injected.surfaces 空 → FAIL。"""
        v = self._gt_version()  # overlay 声明 2 个注入键
        v["gt_injected"] = {}   # 登记缺失
        m = json.loads(json.dumps(self.ok_model))
        self.assertNotEqual(self._run(m, v), 0)

    def test_disclosed_gt_injection_passes(self):
        """overlay 声明与 version.json 登记一致（alias 归并）→ PASS。"""
        v = self._gt_version()
        v["gt_injected"] = {"surfaces": {
            "terminal_pair_span_whitelist": "1 pairs",
            "terminal_levels_injected": "override table",
        }}
        m = json.loads(json.dumps(self.ok_model))
        self.assertEqual(self._run(m, v), 0)

    def test_surfaces_erased_from_version_fails(self):
        """Bug C 负向：注入后抹掉 surfaces（伪装零注入）必须 FAIL。

        此前门禁只查 model.json 里不存在的键 → 恒走「无 z-only 注入面」
        空分支，注入越多的塔越报零注入。现在 overlay 是权威声明面，
        抹掉登记直接拦截。
        """
        v = self._gt_version()  # overlay 声明 whitelist + structure
        # 不写 gt_injected（等于抹掉登记）
        m = json.loads(json.dumps(self.ok_model))
        self.assertNotEqual(self._run(m, v), 0)

    def test_phantom_surface_not_in_overlay_fails(self):
        """反向：version.json 登记了 overlay 未声明的注入面 → FAIL（凭空注入）。"""
        v = self._clean_version()  # 干净 overlay
        v["gt_injected"] = {"surfaces": {
            "use_gt_half_width": "true",
        }}
        m = json.loads(json.dumps(self.ok_model))
        self.assertNotEqual(self._run(m, v), 0)

    def test_missing_version_fails_closed(self):
        """version.json 缺失 → fail-closed（旧恒 PASS 空分支的对立面）。"""
        m = json.loads(json.dumps(self.ok_model))
        self.assertNotEqual(self._run(m), 0)

    def test_merge_prefixed_observation_id_passes(self):
        # 跨册合并前缀 {stem}__obs_... 必须被接受为稳定 ID
        m = json.loads(json.dumps(self.ok_model))
        m["components"]["35A1-JC1-02__obs_x_label_1"] = {
            "id": "35A1-JC1-02__obs_x_label_1", "name": "o",
            "kind": "observation",
            "properties": {"observation_kind": "bar_label"},
        }
        self.assertEqual(self._run(m, self._clean_version()), 0)


class SelfTestContractTest(unittest.TestCase):
    """self_test 门禁自身的结构契约。"""

    def test_gates_exist_and_executable(self):
        st = GATE_DIR / "self_test.py"
        vpi = GATE_DIR / "validate_public_ir.py"
        self.assertTrue(st.exists() and vpi.exists())
        for g in (st, vpi):
            self.assertIn("#!", g.read_text(encoding="utf-8")[:5])

    def test_self_test_quick_actually_runs(self):
        """Bug A 钉子（2026-09-03）：真跑一次 self_test --quick。

        此前只查文件存在/shebang——P5 的 _BAR_ID_RE 回归（M0001 判
        mangled → BOM 维度全跳 → 冒烟 3/5 FAIL）在 685 个测试下全绿
        溜过。结构契约测试测不到行为，这里让门禁 1 进 CI 常绿。
        冒烟跑内置 110kv 示例，无外部依赖，秒级。
        """
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(GATE_DIR / "self_test.py"), "--quick"],
            capture_output=True, text=True, timeout=300, cwd=str(REPO))
        self.assertEqual(
            proc.returncode, 0,
            f"self_test --quick 应全绿，实退 {proc.returncode}：\n"
            f"{proc.stdout[-800:]}\n{proc.stderr[-400:]}")

    def test_bom_classifier_accepts_110kv_m_form(self):
        """Bug A 回归用例：110kv 示例 BOM 首行（M0001）必须判 member。

        _BAR_ID_RE 曾只认国网纯数字件号，M0001 形态被判 mangled，
        member 维度全跳过，r_bom_length/section_match 无数据降 PENDING。
        """
        import csv
        from traceability.intake.tower_bom import classify_bom_row
        with (REPO / "examples" / "tower_110kv_bom.csv").open(
                encoding="utf-8-sig") as f:
            first = next(csv.DictReader(f))
        self.assertEqual(
            classify_bom_row(first["bar_id"], first["section"]), "member",
            f"110kv 首行 {first['bar_id']}/{first['section']} 必须判 member")
        # P5 隔离语义不回归：国网数字件号 / 截面串 / CAD 碎片
        self.assertEqual(classify_bom_row("101", "L40X3"), "member")
        self.assertEqual(classify_bom_row("Q345L63X5", "Q345L63X5"), "mangled")
        self.assertEqual(classify_bom_row("\\M+5B9E6", "50"), "mangled")
        self.assertEqual(classify_bom_row("316", "5M16X40"), "bolt")
        self.assertEqual(classify_bom_row("137", "-6X40"), "plate")


class CaliberNumberConsistencyTest(unittest.TestCase):
    """Bug D 钉子（2026-09-03）：对外主口径数字跨文档一致。

    「同一口径两处数字」是诚实性上最扎眼的问题——此前 214/220 在
    task_brief、SKILL.md、CALIBER_DISCIPLINE 三处打架。这里机械钉住：
    三份文档里的 JC1 dual-view-pure 数字必须互相一致，且都带实测出处
    （commit 号），防止下次改数又漏文件。
    """

    _DOCS = (
        "docs/task_brief_pure60_jc1_zc1.md",
        "domains/angle-tower/SKILL.md",
        "domains/angle-tower/docs/CALIBER_DISCIPLINE.md",
    )

    def test_jc1_dual_view_pure_numbers_agree(self):
        """JC1 A2-dual-view-pure 数字在 task_brief/SKILL/CALIBER_DISCIPLINE
        三处必须一致（Bug D：曾 214 与 220 打架）。"""
        import re
        pat = re.compile(r"TP\s*(\d+)\s*/\s*P\s*([\d.]+)%\s*/\s*R\s*([\d.]+)%")
        seen: dict = {}
        for rel, name in (
            ("domains/angle-tower/SKILL.md", "SKILL"),
            ("domains/angle-tower/docs/CALIBER_DISCIPLINE.md", "DISCIPLINE"),
            ("docs/task_brief_pure60_jc1_zc1.md", "BRIEF"),
        ):
            path = REPO / rel
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if "dual-view-pure" not in line and "35A1-JC1" not in line:
                    continue
                m = pat.search(line)
                if m and ("dual-view-pure" in line
                          or ("35A1-JC1" in line and "TP" in line)):
                    seen.setdefault(name, set()).add(m.groups())
        self.assertTrue(
            len(seen) >= 2, f"应至少在两份文档发现 JC1 主口径数字：{seen}")
        union = set().union(*seen.values())
        self.assertEqual(
            len(union), 1,
            f"JC1 dual-view-pure 数字跨文档不一致（Bug D 回归）：{seen}")


if __name__ == "__main__":
    unittest.main()
