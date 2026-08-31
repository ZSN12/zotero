# -*- coding: utf-8 -*-
"""Phase 1 评测口径统一回归测试。

锁定三件事（计划 P1.1/P1.2/P1.3）：
1. _bar_caliber_class 五层口径判定（origin 优先于 class，stitch 归
   reconstructed，GT 标高辅助归 level_assisted）
2. eval_a2_multi_caliber 的五层 sweep 并列 + by_role/by_origin 统计 +
   match_provenance 追溯字段完整
3. 落盘产物存在性与结构（metrics_multi_caliber / by_role / by_origin /
   evidence_report）
"""

import json
import tempfile
import unittest
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _bar(props, cid="b1"):
    return {"id": cid, "kind": "tower_bar", "properties": props}


def _gt_fixture():
    """2 根 GT 杆：一根近垂直 leg（z 0→1000），一根水平 horiz_x。"""
    return {
        "nodes": {
            "A": [-500.0, 0.0, 0.0], "B": [500.0, 0.0, 0.0],
            "C": [-450.0, 0.0, 1000.0], "D": [450.0, 0.0, 1000.0],
        },
        "bars": [
            {"id": "GT1", "from": "A", "to": "C", "section": "L56X4"},
            {"id": "GT2", "from": "A", "to": "B", "section": "L40X3"},
        ],
    }


def _model_fixture():
    """模型杆：1 根 front 识别 leg（贴 GT1）、1 根 stitch 水平杆（贴 GT2）、
    1 根 GT 标高辅助横隔、1 根纯 FP、1 根 derived（不进任何口径）。"""
    nodes = {}
    comps = {}
    def add_node(nid, x, y, z, extra=None):
        nodes[nid] = {"id": nid, "kind": "tower_node",
                      "properties": {"x": x, "y": y, "z": z, **(extra or {})}}
    add_node("n1", -500.0, 0.0, 0.0)
    add_node("n2", -450.0, 0.0, 1000.0)
    add_node("n3", -500.0, 0.0, 0.0)
    add_node("n4", 500.0, 0.0, 0.0)   # GT2 端点
    add_node("n5", 300.0, 0.0, 500.0)  # FP 杆端点
    comps.update(nodes)
    # front 识别 leg
    comps["bar_leg"] = _bar({
        "geometry_class": "recognized", "geometry_origin": "dxf_geom",
        "face": "f", "role": "LEG", "from_node": "n1", "to_node": "n2",
        "source_file": "S06", "bar_id": "101",
    })
    # stitch 水平杆（继承 recognized class 但 origin=collinear_stitch）
    comps["bar_stitch"] = _bar({
        "geometry_class": "recognized", "geometry_origin": "collinear_stitch",
        "face": "f", "role": "HORIZ", "from_node": "n3", "to_node": "n4",
        "source_file": "S06", "bar_id": "102",
    })
    # GT 标高辅助横隔
    comps["bar_dia"] = _bar({
        "geometry_class": "reconstructed", "geometry_origin": "diaphragm_reconstructed",
        "face": "diaphragm", "role": "HORIZ", "from_node": "n1", "to_node": "n4",
        "level_source": "gt_canonical",
    })
    # 纯 FP（偏离所有 GT）
    comps["bar_fp"] = _bar({
        "geometry_class": "recognized", "geometry_origin": "dxf_geom",
        "face": "f", "role": "DIAG", "from_node": "n2", "to_node": "n5",
        "source_file": "S07", "bar_id": None,
    })
    # derived（corner 展示）
    comps["bar_derived"] = _bar({
        "geometry_class": "derived", "geometry_origin": "derived_4face",
        "face": "corner", "from_node": "n1", "to_node": "n2",
    })
    return {"components": comps}


class CaliberClassTest(unittest.TestCase):
    """P1.3 口径判定。"""

    def test_caliber_classification(self):
        from traceability.eval.metrics import _bar_caliber_class
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "recognized", "geometry_origin": "dxf_geom",
            "face": "f"}), "recognized")
        # stitch：class=recognized 但 origin=collinear_stitch → reconstructed
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "recognized", "geometry_origin": "collinear_stitch",
            "face": "f"}), "reconstructed")
        # 镜像面 dxf_geom → reconstructed（展开重建）
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "reconstructed", "geometry_origin": "dxf_geom",
            "face": "b"}), "reconstructed")
        # GT 标高辅助
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "reconstructed", "geometry_origin": "diaphragm_reconstructed",
            "level_source": "gt_canonical"}), "level_assisted")
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "reconstructed", "geometry_origin": "panel_subdivision",
            "panel_levels_source": "gt_canonical_z_only"}), "level_assisted")
        # dxf 层高的 subdiv → reconstructed
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "reconstructed", "geometry_origin": "panel_subdivision",
            "panel_levels_source": "dxf_derived"}), "reconstructed")
        # 参数化（Phase 5）
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "derived_parametric",
            "geometry_origin": "parametric_extrapolation"}), "parametric")
        # derived 展示
        self.assertEqual(_bar_caliber_class({
            "geometry_class": "derived", "geometry_origin": "derived_4face",
            "face": "corner"}), "derived")


class MultiCaliberEvalTest(unittest.TestCase):
    """P1.1/P1.2/P1.3 完整评测。"""

    def test_multi_caliber_structure_and_numbers(self):
        from traceability.eval.metrics import eval_a2_multi_caliber
        gt = _gt_fixture()
        model = _model_fixture()
        res = eval_a2_multi_caliber(gt, model, view="front",
                                    tols=(500.0,), effective_z_min=None)
        # 五层口径齐全
        self.assertEqual(set(res["calibers"].keys()),
                         {"pure", "reconstructed", "level_assisted", "parametric", "full"})
        cal = res["calibers"]
        # pure = front 识别 2 根（leg + fp）
        self.assertEqual(cal["pure"]["n_model"], 2)
        # reconstructed = + stitch 1 根
        self.assertEqual(cal["reconstructed"]["n_model"], 3)
        # level_assisted = + gt 横隔 1 根
        self.assertEqual(cal["level_assisted"]["n_model"], 4)
        # parametric 空
        self.assertEqual(cal["parametric"]["n_model"], 0)
        # full = level_assisted 同集（parametric 为 0）
        self.assertEqual(cal["full"]["n_model"], 4)
        # GT2（水平）被 stitch 匹配 → reconstructed 层 TP=2, pure 层 TP=1
        self.assertEqual(cal["pure"]["sweep"][0]["tp"], 1)
        self.assertEqual(cal["reconstructed"]["sweep"][0]["tp"], 2)
        self.assertEqual(cal["full"]["sweep"][0]["tp"], 2)

        # by_role：leg 1/1，horiz_x 1/1
        self.assertEqual(res["by_role"]["leg"]["tp"], 1)
        self.assertEqual(res["by_role"]["horiz_x"]["tp"], 1)

        # by_origin：recognized 2（TP1 FP1）、reconstructed 1（TP1）
        self.assertEqual(res["by_origin"]["recognized"]["tp"], 1)
        self.assertEqual(res["by_origin"]["recognized"]["fp"], 1)
        self.assertEqual(res["by_origin"]["reconstructed"]["tp"], 1)

        # P1.1 追溯：TP 记录带完整字段
        tp_records = [r for r in res["match_provenance"] if r["match_status"] == "tp"]
        self.assertEqual(len(tp_records), 2)
        for r in tp_records:
            for key in ("gt_bar_id", "model_component_id", "geometry_origin",
                        "caliber", "member_type", "source_sheet",
                        "distance_mm", "length_ratio", "z_mid_mm"):
                self.assertIn(key, r)
        # stitch 杆的追溯：caliber=reconstructed, origin=collinear_stitch
        st = next(r for r in tp_records
                  if r["model_component_id"] == "bar_stitch")
        self.assertEqual(st["caliber"], "reconstructed")
        self.assertEqual(st["geometry_origin"], "collinear_stitch")
        # FP 记录带 model_component_id 与 caliber
        fp_records = [r for r in res["match_provenance"] if r["match_status"] == "fp"]
        self.assertEqual(len(fp_records), 2)
        self.assertIsNone(fp_records[0]["gt_bar_id"])


class EvalScriptDumpTest(unittest.TestCase):
    """P1 落盘产物。"""

    def test_dump_files_created(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            mp = Path(td) / "model.json"
            mp.write_text(json.dumps(_model_fixture()), encoding="utf-8")
            gp = Path(td) / "gt.json"
            gp.write_text(json.dumps(_gt_fixture()), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(REPO / "scripts/evaluate_ground_truth.py"),
                 str(gp), str(mp), "--view", "front"],
                capture_output=True, text=True, cwd=str(REPO))
            self.assertEqual(r.returncode, 0, r.stderr)
            for name in ("metrics_multi_caliber.json", "metrics_by_role.json",
                         "metrics_by_origin.json", "evidence_report.json"):
                p = Path(td) / name
                self.assertTrue(p.exists(), f"缺落盘产物 {name}")
                data = json.loads(p.read_text(encoding="utf-8"))
                self.assertTrue(data, f"{name} 为空")


if __name__ == "__main__":
    unittest.main()
