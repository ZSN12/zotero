"""阶段 3.1 漏检报告测试（可几何验证口径）。

对应 traceability/eval/miss_report.build_miss_report：
    * FN 四类各 1 例（fragmented / length_mismatch / near_miss_geom / missing）
      外加 one_to_one_conflict（GT 投影重合多杆 vs 模型单杆）；
    * FP 三类各 1 例（duplicate_fp / near_frame / extra）；
    * GT 泄漏 fail-closed raise（阶段0.2）；
    * 汇总计数、JSON 可序列化、空 GT / 空模型边界。

全部使用手写合成 GT/model（无 MLLM/网络）；模型节点只给 x/z
（bars_from_model_2d 回退用 (x, z) 投影，等价于 front 视图 (x, z) 平面），
杆件显式 geometry_class=recognized + view_type=front 以通过 fail-closed 过滤。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _mk_gt(bar_specs):
    """bar_specs: (bar_id, x1, z1, x2, z2[, section]) → GT dict。

    GT 节点为 3D (x, y, z)；front 投影 = (x, z)，与模型节点回退投影同平面。
    """
    nodes = {}
    bars = []
    for k, spec in enumerate(bar_specs):
        bar_id, x1, z1, x2, z2 = spec[:5]
        section = spec[5] if len(spec) > 5 else "L50X5"
        nodes[f"gn{k}a"] = (x1, 0.0, z1)
        nodes[f"gn{k}b"] = (x2, 0.0, z2)
        bars.append({"id": bar_id, "from": f"gn{k}a", "to": f"gn{k}b",
                     "section": section})
    return {"nodes": nodes, "bars": bars}


def _mk_model(bar_specs):
    """bar_specs: (comp_id, x1, z1, x2, z2[, extra_props]) → model dict。"""
    comps = {}
    for k, spec in enumerate(bar_specs):
        cid, x1, z1, x2, z2 = spec[:5]
        extra = spec[5] if len(spec) > 5 else {}
        comps[f"mn{k}a"] = {"kind": "tower_node",
                            "properties": {"x": x1, "y": 0.0, "z": z1}}
        comps[f"mn{k}b"] = {"kind": "tower_node",
                            "properties": {"x": x2, "y": 0.0, "z": z2}}
        props = {"from_node": f"mn{k}a", "to_node": f"mn{k}b",
                 "geometry_class": "recognized",
                 "view_type": "front", "face": "f"}
        props.update(extra)
        comps[cid] = {"kind": "tower_bar", "properties": props}
    return {"components": comps}


def _build(gt, model, **kw):
    from traceability.eval.miss_report import build_miss_report
    return build_miss_report(gt, model, view="front", tol=500.0, **kw)


class FragmentedFnTest(unittest.TestCase):
    """FN fragmented：GT 长杆被 2 根小段拼出但没拼回（各 0.5 覆盖，合计 1.0）。"""

    def test_two_fragments_covering_gt(self):
        gt = _mk_gt([("PM_0001", 0, 0, 6000, 0),      # 目标横杆（被碎片化）
                     ("PM_0002", 0, 0, 0, 4000)])     # 上下文竖杆（精确匹配，撑开 bbox）
        model = _mk_model([("f1", 0, 0, 3000, 0),     # 左半段
                           ("f2", 3000, 0, 6000, 0),  # 右半段
                           ("ctx", 0, 0, 0, 4000)])
        r = _build(gt, model)
        self.assertEqual(r["matched"], 1)
        self.assertEqual(len(r["fn"]), 1)
        fn = r["fn"][0]
        self.assertEqual(fn["gt_bar_id"], "PM_0001")
        self.assertEqual(fn["failure_type"], "fragmented")
        self.assertEqual(fn["evidence"]["overlap_bars"], [0, 1])
        self.assertEqual(fn["evidence"]["coverage_ratio"], 1.0)
        self.assertEqual(r["fn_summary"]["fragmented"], 1)


class LengthMismatchFnTest(unittest.TestCase):
    """FN length_mismatch：1 根 3.5 倍长、同向、完全覆盖 GT 的过度延伸杆。"""

    def test_overextended_bar(self):
        gt = _mk_gt([("PM_0001", 0, 0, 2000, 0),
                     ("PM_0002", 0, 0, 0, 4000)])
        model = _mk_model([("big", -2500, 0, 4500, 0),  # 长 7000 vs GT 2000 → 比值 3.5
                           ("ctx", 0, 0, 0, 4000)])
        r = _build(gt, model)
        self.assertEqual(r["matched"], 1)
        self.assertEqual(len(r["fn"]), 1)
        fn = r["fn"][0]
        self.assertEqual(fn["failure_type"], "length_mismatch")
        self.assertEqual(fn["evidence"]["overlap_bars"], [0])
        self.assertEqual(fn["evidence"]["length_ratio"], 3.5)
        self.assertEqual(r["fn_summary"]["length_mismatch"], 1)


class NearMissGeomFnTest(unittest.TestCase):
    """FN near_miss_geom：平行杆整体偏移 400mm，端点误差 800 > tol=500。"""

    def test_parallel_bar_offset_beyond_tol(self):
        gt = _mk_gt([("PM_0001", 0, 0, 3000, 0),
                     ("PM_0002", 0, 0, 0, 4000)])
        model = _mk_model([("off", 0, 400, 3000, 400),  # 端点误差 400+400=800mm
                           ("ctx", 0, 0, 0, 4000)])
        r = _build(gt, model)
        self.assertEqual(r["matched"], 1)
        self.assertEqual(len(r["fn"]), 1)
        fn = r["fn"][0]
        self.assertEqual(fn["failure_type"], "near_miss_geom")
        self.assertEqual(fn["evidence"]["endpoint_error_mm"], 800.0)
        self.assertLessEqual(fn["evidence"]["nearest_model_mid_mm"], 1500.0)
        self.assertTrue(fn["evidence"]["gate_pass"])
        self.assertEqual(r["fn_summary"]["near_miss_geom"], 1)


class MissingFnTest(unittest.TestCase):
    """FN missing：GT 杆附近（proximity=1500mm）无任何模型杆。"""

    def test_bar_far_away_is_missing_and_extra(self):
        gt = _mk_gt([("PM_0001", 0, 0, 3000, 0),
                     ("PM_0002", 0, 0, 0, 4000)])
        model = _mk_model([("far", 20000, 0, 23000, 0),
                           ("ctx", 0, 0, 0, 4000)])
        r = _build(gt, model)
        self.assertEqual(r["matched"], 1)
        self.assertEqual(len(r["fn"]), 1)
        fn = r["fn"][0]
        self.assertEqual(fn["failure_type"], "missing")
        # nearest_model_mid_mm 对全部模型杆取最小：这里最近的是已匹配的上下文
        # 竖杆（中点 (0,2000)），到 GT 中点 (1500,0) 距离 √(1500²+2000²)=2500，
        # 仍 > proximity=1500，故 missing 成立
        self.assertEqual(fn["evidence"]["nearest_model_mid_mm"], 2500.0)
        self.assertEqual(r["fn_summary"]["missing"], 1)
        # 远处模型杆同时是 FP：远离 GT bbox 外扩带 → extra
        self.assertEqual(r["fp"][0]["failure_type"], "extra")


class OneToOneConflictFnTest(unittest.TestCase):
    """FN one_to_one_conflict：GT 两根投影重合杆（前后面对称），模型只有一根，
    被一对一分配给其中一根，另一根 FN——几何上完全对得上，纯匹配占用。"""

    def test_coincident_gt_bars_single_model_bar(self):
        gt = _mk_gt([("PM_0001", 0, 0, 3000, 0),
                     ("PM_0002", 0, 0, 3000, 0)])
        model = _mk_model([("only", 0, 0, 3000, 0)])
        r = _build(gt, model)
        self.assertEqual(r["matched"], 1)
        self.assertEqual(len(r["fn"]), 1)
        self.assertEqual(r["fp"], [])
        fn = r["fn"][0]
        self.assertEqual(fn["failure_type"], "one_to_one_conflict")
        self.assertEqual(fn["evidence"]["endpoint_error_mm"], 0.0)
        occupied = fn["evidence"]["occupied_by_gt_bar_id"]
        self.assertIn(occupied, ("PM_0001", "PM_0002"))
        self.assertNotEqual(occupied, fn["gt_bar_id"])
        self.assertEqual(r["fn_summary"]["one_to_one_conflict"], 1)


class DuplicateFpTest(unittest.TestCase):
    """FP duplicate_fp：两根近重复 FP，保留离 GT 最近的一根为 representative。"""

    def test_closer_duplicate_kept_as_representative(self):
        gt = _mk_gt([("PM_0001", 0, 0, 3000, 0),
                     ("PM_0002", 0, 0, 0, 4000)])
        model = _mk_model([("good", 0, 0, 3000, 0),      # 匹配 PM_0001
                           ("ctx", 0, 0, 0, 4000),       # 匹配 PM_0002
                           ("d1", 5100, 0, 8100, 0),     # 重复组：离 GT 较远
                           ("d2", 5000, 0, 8000, 0)])    # 重复组：离 GT 较近
        r = _build(gt, model)
        self.assertEqual(r["matched"], 2)
        self.assertEqual(len(r["fp"]), 2)
        by_idx = {e["model_bar_index"]: e for e in r["fp"]}
        # d1=index 2, d2=index 3；d2 离 GT 更近 → representative，d1 → duplicate_fp
        dup = by_idx[2]
        self.assertEqual(dup["failure_type"], "duplicate_fp")
        self.assertEqual(dup["evidence"]["duplicate_of"], 3)
        self.assertGreaterEqual(dup["evidence"]["overlap_ratio"], 0.6)
        self.assertEqual(by_idx[3]["failure_type"], "extra")
        self.assertEqual(r["fp_summary"]["duplicate_fp"], 1)


class NearFrameFpTest(unittest.TestCase):
    """FP near_frame：两端点均落在 GT 投影 bbox 外扩 300mm 的边框带内。"""

    def test_frame_line_and_deep_inside_bar(self):
        gt = _mk_gt([("PM_0001", 0, 0, 3000, 0),
                     ("PM_0002", 0, 0, 0, 4000)])
        model = _mk_model([("good", 0, 0, 3000, 0),
                           ("ctx", 0, 0, 0, 4000),
                           ("frame", -150, 500, -150, 3500),   # bbox 左边外侧 150mm
                           ("inside", 1500, 1500, 1600, 1600)])  # 深入 bbox 内部
        r = _build(gt, model)
        self.assertEqual(r["matched"], 2)
        self.assertEqual(len(r["fp"]), 2)
        by_idx = {e["model_bar_index"]: e for e in r["fp"]}
        self.assertEqual(by_idx[2]["failure_type"], "near_frame")
        self.assertEqual(by_idx[2]["evidence"]["margin_mm"], 300.0)
        self.assertEqual(by_idx[3]["failure_type"], "extra")
        self.assertEqual(r["fp_summary"]["near_frame"], 1)
        self.assertEqual(r["fp_summary"]["extra"], 1)


class GtLeakTest(unittest.TestCase):
    """阶段0.2 fail-closed：模型含 gt_aligned 直接 raise ValueError。"""

    def test_gt_aligned_model_raises(self):
        model = _mk_model([("leak", 0, 0, 1000, 0, {"gt_aligned": True})])
        from traceability.eval.miss_report import build_miss_report
        with self.assertRaises(ValueError) as ctx:
            build_miss_report({"nodes": {}, "bars": []}, model, view="front")
        self.assertIn("阶段0.2", str(ctx.exception))

    def test_canonical_geometry_class_raises(self):
        model = _mk_model([("leak", 0, 0, 1000, 0,
                            {"geometry_class": "canonical"})])
        from traceability.eval.miss_report import build_miss_report
        with self.assertRaises(ValueError):
            build_miss_report({"nodes": {}, "bars": []}, model, view="front")


class SummaryAndSerializationTest(unittest.TestCase):
    """汇总计数正确、条目 schema 完整、输出可 json.dumps（禁 NaN/Inf）。"""

    def test_summary_counts_and_json(self):
        gt = _mk_gt([("PM_0001", 0, 0, 6000, 0, "L63X5"),
                     ("PM_0002", 0, 0, 0, 4000)])
        model = _mk_model([("f1", 0, 0, 3000, 0),
                           ("f2", 3000, 0, 6000, 0),
                           ("ctx", 0, 0, 0, 4000)])
        r = _build(gt, model)
        self.assertEqual(r["view"], "front")
        self.assertEqual(r["tol"], 500.0)
        self.assertEqual(r["n_gt"], 2)
        self.assertEqual(r["n_model"], 3)
        self.assertEqual(r["matched"], 1)
        self.assertEqual(r["precision"], 0.33)
        self.assertEqual(r["recall"], 0.5)
        # 汇总计数：条目数守恒 + 逐类计数
        self.assertEqual(sum(r["fn_summary"].values()), len(r["fn"]))
        self.assertEqual(sum(r["fp_summary"].values()), len(r["fp"]))
        self.assertEqual(r["fn_summary"], {"fragmented": 1, "length_mismatch": 0,
                                           "near_miss_geom": 0,
                                           "one_to_one_conflict": 0, "missing": 0})
        self.assertEqual(r["fp_summary"], {"duplicate_fp": 0, "near_frame": 2,
                                           "extra": 0})
        # JSON 可序列化（allow_nan=False 禁 NaN/Inf）
        payload = json.dumps(r, ensure_ascii=False, allow_nan=False, indent=2)
        self.assertIn("fragmented", payload)

    def test_entry_schema(self):
        gt = _mk_gt([("PM_0001", 0, 0, 3000, 0),
                     ("PM_0002", 0, 0, 0, 4000)])
        model = _mk_model([("off", 0, 400, 3000, 400, {"bar_id": "201",
                                                       "geometry_origin": "dxf"}),
                           ("ctx", 0, 0, 0, 4000)])
        r = _build(gt, model)
        fn_keys = {"gt_bar_id", "section", "x1", "y1", "x2", "y2",
                   "length_mm", "z_mid", "failure_type", "evidence"}
        fp_keys = {"model_bar_index", "bar_id", "geometry_origin",
                   "x1", "y1", "x2", "y2", "length_mm", "failure_type", "evidence"}
        self.assertTrue(fn_keys <= set(r["fn"][0].keys()))
        self.assertTrue(fp_keys <= set(r["fp"][0].keys()))
        self.assertEqual(r["fn"][0]["section"], "L50X5")
        self.assertEqual(r["fn"][0]["z_mid"], 0.0)
        self.assertEqual(r["fp"][0]["bar_id"], "201")
        self.assertEqual(r["fp"][0]["geometry_origin"], "dxf")


class EmptyInputTest(unittest.TestCase):
    """边界：GT 为空 / 模型为空 / 双空。"""

    def test_empty_gt(self):
        gt = {"nodes": {}, "bars": []}
        model = _mk_model([("b0", 0, 0, 1000, 0)])
        r = _build(gt, model)
        self.assertEqual(r["n_gt"], 0)
        self.assertEqual(r["recall"], 0.0)
        self.assertEqual(r["fn"], [])
        # 无 GT 投影 bbox → near_frame 不可能，唯一 FP 归 extra
        self.assertEqual([e["failure_type"] for e in r["fp"]], ["extra"])
        self.assertEqual(r["precision"], 0.0)

    def test_empty_model(self):
        gt = _mk_gt([("PM_0001", 0, 0, 3000, 0)])
        model = {"components": {}}
        r = _build(gt, model)
        self.assertEqual(r["n_model"], 0)
        self.assertEqual(r["precision"], 0.0)
        self.assertEqual(r["recall"], 0.0)
        self.assertEqual([e["failure_type"] for e in r["fn"]], ["missing"])
        self.assertIsNone(r["fn"][0]["evidence"]["nearest_model_mid_mm"])
        self.assertEqual(r["fp"], [])

    def test_both_empty(self):
        r = _build({"nodes": {}, "bars": []}, {"components": {}})
        self.assertEqual(r["matched"], 0)
        self.assertEqual(r["fn"], [])
        self.assertEqual(r["fp"], [])
        self.assertEqual(r["precision"], 0.0)
        self.assertEqual(r["recall"], 0.0)
        json.dumps(r, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
