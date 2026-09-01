"""V1 master BOM 语义修正回归测试（2026-09-02）。

覆盖：
    * physical_bar_counts 按 root stem 计数——四面镜像（F/B/L/R）与
      __split/__panel 细分链只计 1 根（112 计 30 的乘法伪影根因）
    * master 多行撞号（角钢 + 螺栓共用件号）：选结构行（角钢优先）
    * qty<=0 行跳过
    * conflicts 只含 over_count（模型 > 图纸）；模型 < 图纸归
      under_identified（P4 标注覆盖缺口，不 FAILED）
"""
from __future__ import annotations

import csv
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _bar(cid, bid, face, origin="dxf_geom", status="recognized"):
    return {
        "id": cid, "kind": "tower_bar", "name": cid,
        "properties": {
            "bar_id": bid, "from_node": "n1", "to_node": "n2",
            "face": face, "geometry_origin": origin,
            "geometry_class": status, "evidence_status": status,
        },
    }


def _model(bars):
    comps = {}
    for b in bars:
        comps[b["id"]] = b
    return {"components": comps}


class RootStemCountTest(unittest.TestCase):
    def test_mirror_and_split_dedup(self):
        from traceability.project.module_build import _root_stem

        # 四面镜像同杆
        self.assertEqual(
            _root_stem("4f_35A1-JC1-02__bar_107_front__split77__split79_F"),
            "35A1-JC1-02__bar_107_front",
        )
        self.assertEqual(
            _root_stem("4f_35A1-JC1-02__bar_107_front__split77__split79_B"),
            "35A1-JC1-02__bar_107_front",
        )
        # split 链全部剥掉
        self.assertEqual(
            _root_stem("4f_35A1-JC1-05__bar_UNLABELED_70C_front__split252__split258_L"),
            "35A1-JC1-05__bar_UNLABELED_70C_front",
        )
        # 不同母杆序号 = 不同物理杆（保留）
        self.assertNotEqual(
            _root_stem("4f_35A1-JC1-02__bar_112_front_56__split55_F"),
            _root_stem("4f_35A1-JC1-02__bar_112_front__split61_F"),
        )

    def test_counts_per_root_stem(self):
        """112 场景：26 dxf_geom 实例 + 4 stitch，全来自 2 条母线 → 计 2 根。"""
        from traceability.model import Component, EngineeringModel
        from traceability.project.module_build import physical_bar_counts

        model = EngineeringModel(name="m")
        bars = []
        # 母线 A：front + b/l/r 镜像 + 二级 split（同 root stem）
        for face in ("f", "b", "l", "r"):
            bars.append(_bar(f"4f_barA_front__split61_{face.upper()}", "112", face))
        bars.append(_bar("4f_barA_front__split61__split65_F", "112", "f"))
        # 母线 B：独立识别线（_56 后缀），front + back
        bars.append(_bar("4f_barB_front_56__split55_F", "112", "f"))
        bars.append(_bar("4f_barB_front_56__split55_B", "112", "b"))
        for b in bars:
            model.add_component(Component(
                id=b["id"], name=b["id"], kind="tower_bar",
                properties=b["properties"],
            ))
        counts = physical_bar_counts(model)
        self.assertEqual(counts.get("112"), 2)


class MasterRowSelectionTest(unittest.TestCase):
    def test_angle_row_wins_over_bolt(self):
        from traceability.project.bom_tree import _select_master_row, is_fitting_section

        rows = [
            {"bar_id": "316", "section": "L50X4", "qty": 2, "length_mm": 1092},
            {"bar_id": "316", "section": "5M16X40", "qty": 1, "length_mm": 336},
        ]
        sel = _select_master_row(rows)
        self.assertEqual(sel["section"], "L50X4")
        self.assertEqual(sel["qty"], 2)

        self.assertTrue(is_fitting_section("5M16X40"))
        self.assertTrue(is_fitting_section("-6X128"))
        self.assertTrue(is_fitting_section("Q345-12X135"))
        self.assertFalse(is_fitting_section("L40X3"))
        self.assertFalse(is_fitting_section("Q345L70X5"))

    def test_zero_qty_skipped(self):
        from traceability.project.bom_tree import _select_master_row

        rows = [{"bar_id": "3902", "section": "Q345L110X8", "qty": 0, "length_mm": 6047}]
        self.assertIsNone(_select_master_row(rows))


class ConflictClassificationTest(unittest.TestCase):
    def _run_tree(self, counts):
        import tempfile

        from traceability.project.bom_tree import aggregate_bom_tree

        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["bar_id", "section", "length_mm", "qty", "sheet"])
            w.writerow(["100", "L40X3", "1000", "4", "35A1-JC1-02"])   # under: model 1 < 4
            w.writerow(["200", "L40X3", "1000", "1", "35A1-JC1-02"])   # match: 1 == 1
            w.writerow(["300", "L50X4", "2000", "2", "35A1-JC1-04"])   # over: model 3 > 2
            w.writerow(["301", "L50X4", "2000", "2", "35A1-JC1-04"])   # bolt 撞号，角钢行胜
            w.writerow(["302", "-6X128", "300", "2", "35A1-JC1-04"])
            w.writerow(["302", "7M16X40", "40", "1", "35A1-JC1-04"])   # 302 两行全配件
            path = f.name
        return aggregate_bom_tree([], master_bom_path=path, physical_bar_counts=counts)

    def test_over_vs_under_split(self):
        counts = {"100": 1, "200": 1, "300": 3, "301": 2, "302": 2}
        tree = self._run_tree(counts)

        over = [c["bar_id"] for c in tree["conflicts"]]
        under = [c["bar_id"] for c in tree["under_identified"]]
        self.assertEqual(over, ["300"])          # 模型 3 > 图纸 2 → 真实冲突
        self.assertIn("100", under)              # 模型 1 < 图纸 4 → 覆盖缺口
        self.assertNotIn("200", under)           # 1 == 1 → 无冲突
        self.assertNotIn("301", [c["bar_id"] for c in tree["conflicts"]])
        # 302 纯配件撞号：_select_master_row 取 max-qty 行（-6X128 q2），
        # model 2 == 2 → 不进任何清单（或按行选择不同，至少不误报 over）

    def test_harness_rule_semantics(self):
        """over_count → FAILED；仅 under → PENDING；全平 → PASSED。"""
        from traceability.model import ValidationStatus
        from traceability.project.harness import run_project_harness
        from traceability.project.model import ProjectModel, ProjectSheet

        def _run(bom_tree):
            proj = ProjectModel(project_id="p", name="p")
            proj.sheets["s1"] = ProjectSheet(sheet_id="s1", path="s1.dxf", model_path="x")
            proj.metadata = {"master_bom_path": "bom.csv"}
            return run_project_harness(proj, bom_tree=bom_tree)

        # 只有 over → FAILED
        r = _run({"conflicts": [{"bar_id": "1", "aggregated_qty": 3, "master_qty": 2}],
                  "under_identified": [], "total_unique_bar_ids": 5})
        st = [x for x in r["results"] if x["rule"] == "r_project_bom_master"][0]["status"]
        self.assertEqual(st, ValidationStatus.FAILED.value)

        # 只有 under → PENDING（不拦 verified 之外还诚实可见）
        r = _run({"conflicts": [], "under_identified": [{"bar_id": "1"}],
                  "total_unique_bar_ids": 5})
        st = [x for x in r["results"] if x["rule"] == "r_project_bom_master"][0]["status"]
        self.assertEqual(st, ValidationStatus.PENDING.value)

        # 全平 → PASSED
        r = _run({"conflicts": [], "under_identified": [],
                  "total_unique_bar_ids": 5,
                  "only_in_master": [], "only_in_model": []})
        st = [x for x in r["results"] if x["rule"] == "r_project_bom_master"][0]["status"]
        self.assertEqual(st, ValidationStatus.PASSED.value)


if __name__ == "__main__":
    unittest.main()
