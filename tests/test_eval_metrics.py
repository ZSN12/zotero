"""评测核心失败测试（M0 评测可信）。

按清单「每阶段先补失败测试，再改实现」：
    * Hungarian 一对一最优匹配（替代贪心，验证不产生次优误配）
    * tolerance sweep PR（50/100/200/500mm）
    * 三态语义：recognized / mirrored / derived 的 P/R 归属
    * derived 构件（corner_leg/diaphragm/镜像面）不进 recognition P/R
    * 装配回退：真 M1-M6 失败不得用 demo 冒充成功
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class HungarianMatchTest(unittest.TestCase):
    """P1 评测重写：Hungarian 一对一最优匹配替代中点贪心。"""

    def test_one_to_one_optimal_matching(self):
        from traceability.eval.metrics import hungarian_match, segment_cost

        gt = [(0.0, 0.0, 10.0, 0.0), (0.0, 5.0, 10.0, 5.0), (0.0, 10.0, 10.0, 10.0)]
        model = [(0.0, 0.0, 10.0, 0.0), (0.0, 5.0, 10.0, 5.0), (100.0, 100.0, 110.0, 100.0)]
        matched, un_gt, un_m = hungarian_match(gt, model, segment_cost, max_cost=50.0)
        # 前两根精确匹配，第三根模型杆错位（100mm 外）
        self.assertEqual(set(matched), {(0, 0), (1, 1)})
        self.assertEqual(un_gt, [2])
        self.assertEqual(un_m, [2])

    def test_hungarian_avoids_greedy_suboptimal(self):
        """贪心会把 GT0 抢走本属 GT1 的模型杆，Hungarian 全局最优不会。"""
        from traceability.eval.metrics import hungarian_match, segment_cost

        # GT0 和 GT1 都很靠近 model0，但 model0 只能配一个；
        # model1 只靠近 GT1。贪心会 GT0→model0（更近），导致 GT1 只能勉强配 model1。
        # Hungarian 全局最优应 GT0→model1(若更合理)或至少不产生 >1 的错配。
        gt = [(0.0, 0.0, 10.0, 0.0), (1.0, 0.0, 11.0, 0.0)]
        model = [(0.0, 0.0, 10.0, 0.0), (50.0, 0.0, 60.0, 0.0)]
        matched, un_gt, un_m = hungarian_match(gt, model, segment_cost, max_cost=50.0)
        # model1 (50mm 外) 应不匹配任何 GT；至多 1 对匹配
        self.assertLessEqual(len(matched), 1)
        # model1 必须在 unmatched_model 里
        self.assertIn(1, un_m)

    def test_empty_gt_and_model(self):
        from traceability.eval.metrics import hungarian_match, segment_cost
        matched, un_gt, un_m = hungarian_match([], [], segment_cost, 50.0)
        self.assertEqual(matched, [])
        self.assertEqual(un_gt, [])
        self.assertEqual(un_m, [])


class ToleranceSweepTest(unittest.TestCase):
    """P1 评测重写：tolerance sweep 而非单一容差。"""

    def test_sweep_pr_curve(self):
        from traceability.eval.metrics import eval_segment_pr, segment_cost

        gt = [(0.0, 0.0, 10.0, 0.0), (0.0, 5.0, 10.0, 5.0), (0.0, 10.0, 10.0, 10.0)]
        model = [(0.0, 0.0, 10.0, 0.0), (0.0, 5.0, 10.0, 5.0), (100.0, 100.0, 110.0, 100.0)]
        r = eval_segment_pr(gt, model, segment_cost, tols=(10.0, 50.0, 200.0))
        # 默认容差（最后一个 = 200）下，第三根模型杆仍错位 100mm → 匹配 2 对
        last = r["sweep"][-1]
        self.assertEqual(last["tp"], 2)
        self.assertEqual(last["fp"], 1)
        self.assertEqual(last["fn"], 1)
        self.assertAlmostEqual(last["precision"], 2 / 3, places=3)
        self.assertAlmostEqual(last["recall"], 2 / 3, places=3)
        # sweep 含多个容差
        self.assertEqual([s["tol"] for s in r["sweep"]], [10.0, 50.0, 200.0])


class SemanticFreezeTest(unittest.TestCase):
    """P0 语义冻结：recognized / mirrored / derived 三态。"""

    def test_derived_excluded_from_recognition(self):
        from traceability.eval.metrics import (
            is_derived_bar, is_recognized_bar, is_physical_bar,
        )
        # recognized：front 面直接识别
        self.assertTrue(is_recognized_bar({"evidence_status": "recognized", "face": "f"}))
        self.assertFalse(is_derived_bar({"evidence_status": "recognized", "face": "f"}))
        # mirrored：镜像面 B/L/R —— 非 derived（进 physical），但非 recognized
        self.assertFalse(is_derived_bar({"evidence_status": "mirrored", "face": "b"}))
        self.assertFalse(is_recognized_bar({"evidence_status": "mirrored", "face": "b"}))
        self.assertTrue(is_physical_bar({"evidence_status": "mirrored", "face": "b"}))
        # derived：corner_leg / diaphragm / center —— 不进任何 P/R
        self.assertTrue(is_derived_bar({"evidence_status": "derived", "diaphragm": True}))
        self.assertTrue(is_derived_bar({"corner_leg": True}))
        self.assertTrue(is_derived_bar({"face": "diaphragm"}))
        self.assertFalse(is_physical_bar({"evidence_status": "derived", "diaphragm": True}))

    def test_corner_leg_and_diaphragm_excluded(self):
        """清单核心：整高合成角腿与自动 diaphragm 不计物理 P/R。"""
        from traceability.eval.metrics import is_derived_bar
        self.assertTrue(is_derived_bar({"corner_leg": True, "evidence_status": "mirrored"}))
        self.assertTrue(is_derived_bar({"diaphragm": True}))
        self.assertTrue(is_derived_bar({"auto_diaphragm": True}))

    def test_model_extraction_filters_by_mode(self):
        from traceability.eval.metrics import bars_from_model_3d

        model = {"components": {}}
        def add_node(cid, x, y, z):
            model["components"][cid] = {"kind": "tower_node", "properties": {"x": x, "y": y, "z": z}}
        def add_bar(cid, f, t, **props):
            model["components"][cid] = {"kind": "tower_bar", "properties": {"from_node": f, "to_node": t, **props}}

        add_node("n1", 0, 0, 1000); add_node("n2", 1000, 0, 1000)
        add_node("n5", 0, -500, 1000); add_node("n6", 1000, -500, 1000)
        add_node("n3", 0, 500, 1000); add_node("n4", 1000, 500, 1000)
        add_bar("b_f", "n1", "n2", evidence_status="recognized", face="f")
        add_bar("b_b", "n5", "n6", evidence_status="mirrored", face="b")
        add_bar("b_d", "n1", "n3", evidence_status="derived", face="diaphragm", diaphragm=True)

        rec = bars_from_model_3d(model, mode="recognition")
        phy = bars_from_model_3d(model, mode="physical")
        self.assertEqual(len(rec), 1, "recognition 应只含 front 面 1 根")
        self.assertEqual(len(phy), 2, "physical 应含 recognized+mirrored 共 2 根")

    def test_reconstructed_is_physical_but_not_recognized(self):
        """阶段0：reconstructed（确定性重建）进 physical P/R，不进 recognition P/R。"""
        from traceability.eval.metrics import (
            is_recognized_bar, is_reconstructed_bar, is_physical_bar,
        )
        # mirrored 是 reconstructed 的一种实现
        self.assertTrue(is_reconstructed_bar({"evidence_status": "mirrored", "face": "b"}))
        self.assertTrue(is_reconstructed_bar({"evidence_status": "reconstructed"}))
        self.assertFalse(is_reconstructed_bar({"evidence_status": "recognized", "face": "f"}),
                         "recognized 不是 reconstructed")
        self.assertFalse(is_reconstructed_bar({"evidence_status": "derived", "diaphragm": True}),
                         "derived 不是 reconstructed")
        self.assertFalse(is_reconstructed_bar({"gt_aligned": True}), "canonical 不是 reconstructed")
        # reconstructed 是 physical 杆件，但不是 recognized
        self.assertTrue(is_physical_bar({"evidence_status": "reconstructed"}))
        self.assertFalse(is_recognized_bar({"evidence_status": "reconstructed"}))
        # 兼容旧数据：generated_4face 且非 recognized 的非 derived 杆件算 reconstructed
        self.assertTrue(is_reconstructed_bar({"generated_4face": True, "evidence_status": "mirrored"}))

    def test_m3_physical_semantic_split(self):
        """M3 语义分解：physical = recognized + reconstructed，各自计数。"""
        from traceability.eval.metrics import eval_m3_physical_3d
        gt = {"nodes": {"n1": (0.0, 0.0, 0.0), "n2": (1000.0, 0.0, 0.0)},
              "bars": [{"id": "PM_0001", "from": "n1", "to": "n2"}]}
        model = {"components": {
            "n1": {"kind": "tower_node", "properties": {"x": 0.0, "y": 0.0, "z": 0.0}},
            "n2": {"kind": "tower_node", "properties": {"x": 1000.0, "y": 0.0, "z": 0.0}},
            "b_rec": {"kind": "tower_bar", "properties": {"from_node": "n1", "to_node": "n2",
                      "evidence_status": "recognized", "face": "f"}},
            "b_recon": {"kind": "tower_bar", "properties": {"from_node": "n1", "to_node": "n2",
                      "evidence_status": "reconstructed", "face": "b"}},
            "b_der": {"kind": "tower_bar", "properties": {"from_node": "n1", "to_node": "n2",
                      "evidence_status": "derived", "diaphragm": True}},
        }}
        r = eval_m3_physical_3d(gt, model)
        sem = r["model_semantic"]
        self.assertEqual(sem["recognized"], 1)
        self.assertEqual(sem["reconstructed"], 1, "reconstructed 应单独计数，derived 不计入")


class GtLeakageTest(unittest.TestCase):
    """P0 阶段 0.2：GT 泄漏检测——GT 对齐过的模型拒绝评测。"""

    def test_model_has_gt_alignment_detected(self):
        from traceability.eval.metrics import model_has_gt_alignment

        clean = {"components": {"b": {"kind": "tower_bar",
                                      "properties": {"evidence_status": "recognized"}}}}
        self.assertFalse(model_has_gt_alignment(clean))

        polluted = {"components": {"b": {"kind": "tower_bar",
                                         "properties": {"gt_aligned": True}}}}
        self.assertTrue(model_has_gt_alignment(polluted))

    def test_canonical_bar_excluded_from_physical(self):
        from traceability.eval.metrics import is_physical_bar, is_canonical_bar
        # GT 对齐 / canonical 权威拓扑：不进 physical P/R
        self.assertTrue(is_canonical_bar({"gt_aligned": True}))
        self.assertTrue(is_canonical_bar({"geometry_class": "canonical"}))
        self.assertTrue(is_canonical_bar({"geometry_origin": "gim"}))
        self.assertFalse(is_physical_bar({"gt_aligned": True}))
        self.assertFalse(is_physical_bar({"geometry_class": "canonical"}))

    def test_eval_rejects_gt_aligned_model(self):
        from traceability.eval.metrics import eval_m3_physical_3d, model_has_gt_alignment
        gt = {"nodes": {"n1": (0.0, 0.0, 0.0), "n2": (1000.0, 0.0, 0.0)},
              "bars": [{"id": "PM_0001", "from": "n1", "to": "n2"}]}
        # 模型被 GT 对齐污染：所有杆件带 gt_aligned=True
        model = {"components": {
            "n1": {"kind": "tower_node", "properties": {"x": 0.0, "y": 0.0, "z": 0.0}},
            "n2": {"kind": "tower_node", "properties": {"x": 1000.0, "y": 0.0, "z": 0.0}},
            "b": {"kind": "tower_bar",
                  "properties": {"from_node": "n1", "to_node": "n2", "gt_aligned": True}},
        }}
        self.assertTrue(model_has_gt_alignment(model), "评测前必须先检测 GT 污染")
        # 即使不显式拒绝，is_physical_bar 也应把 GT 杆件排除（n_model=0）
        r = eval_m3_physical_3d(gt, model)
        self.assertEqual(r["n_model"], 0, "GT 对齐杆件必须被排除出 physical P/R")

    def test_expand_4_face_no_gt_half_width_marks_nothing(self):
        """GT 隔离：默认（无 use_gt_half_width）四面展开不注入 GT 半宽，不打 gt_aligned。"""
        from traceability.model import Component, EngineeringModel, SourceRef, SourceType
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model

        m = EngineeringModel(name="test")
        m.add_component(Component(
            id="drawing_file", name="df", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "35A1-JC1-02.dxf"),
            properties={"view_kinds": ["front"]},
        ))
        for nid, (x, z) in {"L1": (1500.0, 0.0), "L2": (1500.0, 3000.0),
                            "R1": (-1500.0, 0.0), "R2": (-1500.0, 3000.0)}.items():
            m.add_component(Component(id=nid, name=nid, kind="tower_node",
                properties={"x": x, "y": 0.0, "z": z, "view_type": "front", "solve_status": "solved"}))
        m.add_component(Component(id="leg", name="leg", kind="tower_bar",
            properties={"from_node": "L1", "to_node": "L2", "view_type": "front",
                        "bar_id": "105", "geometry_origin": "dxf_geom"}))

        expand_4_face_symmetry_model(m, None)
        aligned = [c for c in m.components.values()
                   if c.kind in ("tower_bar", "tower_node") and c.properties.get("gt_aligned")]
        self.assertEqual(len(aligned), 0, "无 GT 半宽时不得打 gt_aligned")

    def test_expand_4_face_with_gt_half_width_marks_gt_aligned(self):
        """GT 隔离：显式 use_gt_half_width=true 时，产物全部打 gt_aligned（评测拒绝）。"""
        import json
        import tempfile
        from pathlib import Path
        from traceability.model import Component, EngineeringModel, SourceRef, SourceType
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model

        m = EngineeringModel(name="test")
        m.add_component(Component(
            id="drawing_file", name="df", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "35A1-JC1-02.dxf"),
            properties={"view_kinds": ["front"]},
        ))
        for nid, (x, z) in {"L1": (1500.0, 0.0), "L2": (1500.0, 3000.0),
                            "R1": (-1500.0, 0.0), "R2": (-1500.0, 3000.0)}.items():
            m.add_component(Component(id=nid, name=nid, kind="tower_node",
                properties={"x": x, "y": 0.0, "z": z, "view_type": "front", "solve_status": "solved"}))
        m.add_component(Component(id="leg", name="leg", kind="tower_bar",
            properties={"from_node": "L1", "to_node": "L2", "view_type": "front",
                        "bar_id": "105", "geometry_origin": "dxf_geom"}))

        with tempfile.TemporaryDirectory() as d:
            ov = Path(d) / "overlay.json"
            ov.write_text(json.dumps({"use_gt_half_width": True}))
            expand_4_face_symmetry_model(m, str(ov))
        total = [c for c in m.components.values()
                 if c.kind in ("tower_bar", "tower_node")]
        aligned = [c for c in total if c.properties.get("gt_aligned")]
        self.assertEqual(len(aligned), len(total), "GT 半宽开启时所有重建构件应打 gt_aligned")


class AssemblyFallbackTest(unittest.TestCase):
    """P1 装配闭合：真 M1-M6 失败不得用 demo 冒充成功。"""

    def test_m1_m6_failure_not_fallthrough_to_demo(self):
        from traceability.project import delivery
        from traceability.model import EngineeringModel
        from unittest import mock

        merged = EngineeringModel(name="merged")
        overlay = {"enable_module_assembly": True, "module_definitions": "m1_m6"}

        # _select_assembly 内部 import module_build 的两个函数，直接 mock 该模块符号
        with mock.patch(
            "traceability.project.module_build.try_assembly_m1_m6_from_merged",
            return_value=None,
        ) as m6, mock.patch(
            "traceability.project.module_build.try_assembly_from_merged",
        ) as demo:
            info, fallback = delivery._select_assembly(merged, None, overlay)

            # demo 回退函数绝不能被调用（配置了 module_definitions）
            demo.assert_not_called()
            self.assertIsNotNone(info)
            self.assertFalse(info["enabled"])
            self.assertIn("error", info)
            self.assertFalse(fallback, "M1-M6 失败不应回退 demo")

    def test_demo_fallback_only_without_module_definitions(self):
        from traceability.project import delivery
        from traceability.model import EngineeringModel
        from unittest import mock

        merged = EngineeringModel(name="merged")
        overlay = {"enable_module_assembly": True, "assembly_demo_z_split": 0.5}

        with mock.patch(
            "traceability.project.module_build.try_assembly_from_merged",
            return_value={"model": object(), "mode": "assembly_demo_z_split"},
        ) as demo, mock.patch(
            "traceability.project.module_build.try_assembly_m1_m6_from_merged",
        ) as m6:
            info, fallback = delivery._select_assembly(merged, None, overlay)

            m6.assert_not_called()
            self.assertIsNotNone(info)
            self.assertTrue(fallback, "无 module_definitions 时允许 demo 回退")


class FourMetricsIndependentTest(unittest.TestCase):
    """P1 四套指标不可混算：A1/A2/A3/M3 各自独立口径。"""

    def _gt(self):
        # 3 根 GT 杆件（含件号）
        return {
            "nodes": {
                "n1": (0.0, 0.0, 0.0), "n2": (1000.0, 0.0, 0.0),
                "n3": (0.0, 0.0, 1000.0), "n4": (1000.0, 0.0, 1000.0),
                "n5": (0.0, 0.0, 2000.0), "n6": (1000.0, 0.0, 2000.0),
            },
            "bars": [
                {"id": "PM_0001", "from": "n1", "to": "n2", "section": "L90X6"},
                {"id": "PM_0002", "from": "n3", "to": "n4", "section": "L90X6"},
                {"id": "PM_0003", "from": "n5", "to": "n6", "section": "L90X6"},
            ],
        }

    def _model(self, bar_ids):
        # 3 根 recognized 杆件（与 GT 几何一致，但件号可错）
        comps = {}
        for i in range(3):
            z = i * 1000.0
            comps[f"n{i}a"] = {"kind": "tower_node",
                               "properties": {"x": 0.0, "y": 0.0, "z": z}}
            comps[f"n{i}b"] = {"kind": "tower_node",
                               "properties": {"x": 1000.0, "y": 0.0, "z": z}}
        for i, bid in enumerate(bar_ids):
            comps[f"b{i}"] = {"kind": "tower_bar",
                              "properties": {"from_node": f"n{i}a", "to_node": f"n{i}b",
                                             "bar_id": bid, "evidence_status": "recognized",
                                             "face": "f"}}
        return {"components": comps}

    def test_a1_exact_match_independent(self):
        from traceability.eval.metrics import eval_a1_labels

        gt = self._gt()
        # 模型识别出 PM_0001 / PM_0002，漏 PM_0003，多了 PM_9999
        model = self._model(["PM_0001", "PM_0002", "PM_9999"])
        r = eval_a1_labels(gt, model)
        self.assertEqual(r["tp"], 2)
        self.assertEqual(r["fp"], 1)
        self.assertEqual(r["fn"], 1)
        self.assertAlmostEqual(r["precision"], 2 / 3, places=3)
        self.assertAlmostEqual(r["recall"], 2 / 3, places=3)

    def test_a1_with_id_mapping(self):
        from traceability.eval.metrics import eval_a1_labels

        gt = self._gt()
        # 模型用图纸数字件号 105/108/109，需要映射到 GT 命名空间
        model = self._model(["105", "108", "109"])
        mapping = {"105": "PM_0001", "108": "PM_0002", "109": "PM_0003"}
        r = eval_a1_labels(gt, model, id_mapping=mapping)
        self.assertEqual(r["tp"], 3, "id_mapping 应把数字件号映射到 GT 件号")
        self.assertEqual(r["exact_match_rate"], 1.0)

    def test_a3_association_detects_wrong_label(self):
        from traceability.eval.metrics import eval_a3_association

        gt = self._gt()
        # 几何全对，但件号错位（PM_0001 贴到了第二根杆上）
        model = self._model(["PM_0002", "PM_0001", "PM_0003"])
        r = eval_a3_association(gt, model)
        self.assertEqual(r["matched_pairs"], 3, "几何应全匹配")
        self.assertEqual(r["correct_association"], 1, "仅第三根件号正确关联")
        self.assertAlmostEqual(r["association_rate"], 1 / 3, places=3)


if __name__ == "__main__":
    unittest.main()
