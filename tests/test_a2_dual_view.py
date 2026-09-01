"""A2 双视图联合口径（eval_a2_dual_view）单元测试。

覆盖：
  * side 视图投影坐标轴修复（view='side' → (y, z)，非 (x, z)）；
  * 杆粒度并集语义：GT 杆任一视图匹配即 TP；模型杆全视图未匹配才 FP；
  * 横隔杆跨视图去重（cid 只计一次）；
  * b 面排除（side = l/r 面）；
  * sweep 结构完整。
"""

import copy
import unittest

from traceability.eval.metrics import (
    eval_a2_dual_view,
    bars_from_model_2d,
)


def _gt():
    return {
        "nodes": {
            "g1": (2000.0, 2000.0, 10000.0),
            "g2": (0.0, 2000.0, 14000.0),
            "g3": (2000.0, 0.0, 14000.0),
        },
        "bars": [
            # front 投影斜线 (2000,10000)->(0,14000)（y 不变）
            {"id": "GT_F", "from": "g1", "to": "g2", "section": "L100x7"},
            # side 投影斜线 (2000,10000)->(0,14000)（y 变化）
            {"id": "GT_S", "from": "g1", "to": "g3", "section": "L100x7"},
        ],
    }


def _model(f_bars, l_bars=None, diaphragm=None):
    """构造最小 model.json：f 面 + l/b 面 + 可选横隔。"""
    nodes = {
        "n1": {"kind": "tower_node", "properties": {"x": 2000.0, "y": 2000.0, "z": 10000.0}},
        "n2": {"kind": "tower_node", "properties": {"x": 0.0, "y": 2000.0, "z": 14000.0}},
        "n3": {"kind": "tower_node", "properties": {"x": 2000.0, "y": 0.0, "z": 14000.0}},
        # 远离 GT 的孤立节点（构造必然 FP 的杆）
        "n4": {"kind": "tower_node", "properties": {"x": 6000.0, "y": 6000.0, "z": 11000.0}},
    }
    comps = dict(nodes)
    for cid, f_, t_, face in f_bars:
        comps[cid] = {"kind": "tower_bar", "properties": {
            "from_node": f_, "to_node": t_, "face": face, "role": "DIAG",
            "geometry_class": "recognized", "geometry_origin": "dxf_geom",
            "evidence_status": "recognized", "source_file": "S1",
        }}
    for cid, f_, t_, face in (l_bars or []):
        comps[cid] = {"kind": "tower_bar", "properties": {
            "from_node": f_, "to_node": t_, "face": face, "role": "DIAG",
            "geometry_class": "recognized", "geometry_origin": "dxf_geom",
            "evidence_status": "recognized", "source_file": "S1",
        }}
    if diaphragm:
        cid, f_, t_ = diaphragm
        comps[cid] = {"kind": "tower_bar", "properties": {
            "from_node": f_, "to_node": t_, "face": "diaphragm", "role": "DIA",
            "geometry_class": "reconstructed",
            "geometry_origin": "diaphragm_reconstructed",
            "evidence_status": "reconstructed", "source_file": "S1",
        }}
    return {"components": comps}


class TestSideProjection(unittest.TestCase):
    def test_side_uses_yz_plane(self):
        """view='side' 必须 (y,z) 投影：l 面杆（x=const, y 变化）在 side
        投影为斜线；修复前错取 (x,z) 投影成竖线。"""
        m = _model([], l_bars=[("bar_l", "n1", "n2", "l")])
        # n1=(2000,2000,10000), n2=(0,2000,14000) —— l 面
        out = bars_from_model_2d(m, view="side", mode="physical")
        self.assertEqual(len(out), 1)
        seg = out[0][0]
        # (y,z)：n1→(2000,10000), n2→(2000,14000)——竖线
        self.assertAlmostEqual(seg[0], 2000.0)
        self.assertAlmostEqual(seg[2], 2000.0)
        self.assertAlmostEqual(seg[1], 10000.0)
        self.assertAlmostEqual(seg[3], 14000.0)

    def test_front_still_xz(self):
        m = _model([("bar_f", "n1", "n2", "f")])
        out = bars_from_model_2d(m, view="front", mode="physical")
        seg = out[0][0]
        # (x,z)：n1→(2000,10000), n2→(0,14000)，排序后 (0,14000)->(2000,10000)
        self.assertAlmostEqual(seg[0], 0.0)
        self.assertAlmostEqual(seg[1], 14000.0)
        self.assertAlmostEqual(seg[2], 2000.0)
        self.assertAlmostEqual(seg[3], 10000.0)


class TestDualViewUnion(unittest.TestCase):
    def test_union_recall(self):
        """f 面杆命中 GT_F（front），l 面杆命中 GT_S（side）→ 双杆全召回；
        单 front 视图只能召回 GT_F。"""
        m = _model(
            [("bar_f", "n1", "n2", "f")],
            l_bars=[("bar_l", "n1", "n3", "l")],
        )
        r = eval_a2_dual_view(_gt(), m, tols=[500.0])
        full = r["calibers"]["full"]
        self.assertEqual(full["sweep"][0]["tp"], 2)
        self.assertEqual(full["sweep"][0]["fp"], 0)
        self.assertAlmostEqual(full["sweep"][0]["recall"], 1.0)

    def test_diaphragm_dedup_across_views(self):
        """横隔投影进两视图，cid 只计一次模型杆（口径为累计语义）。"""
        m = _model([("bar_f", "n1", "n2", "f")], diaphragm=("dia1", "n2", "n3"))
        r = eval_a2_dual_view(_gt(), m, tols=[500.0])
        # full 模型杆 = bar_f + dia1 = 2（dia1 不因两视图重复计数）
        self.assertEqual(r["calibers"]["full"]["n_model"], 2)
        # reconstructed 是累计口径（_CALIBER_SETS 含 recognized）：bar_f + dia1 = 2
        self.assertEqual(r["calibers"]["reconstructed"]["n_model"], 2)
        # pure 只含 recognized：bar_f = 1
        self.assertEqual(r["calibers"]["pure"]["n_model"], 1)
        # per_view 审计：dia1 同时出现在两个视图
        for v in ("front", "side"):
            self.assertIn("diaphragm", r["per_view"][v]["faces"])

    def test_b_face_excluded_from_side(self):
        """b 面杆不进 side 视图（y-z 竖线与腿重合，1:1 失衡）。"""
        m = _model([("bar_f", "n1", "n2", "f")],
                   l_bars=[("bar_b", "n1", "n3", "b")])
        r = eval_a2_dual_view(_gt(), m, tols=[500.0])
        # P2.5 对称化后：b 面杆投影进 front（4 面展开物理杆的 (x,z) 投影，
        # 匹配 GT 重复计数口径）；side 仍显式排除 b（y-z 竖线与腿重合，
        # 1:1 失衡，见 eval_a2_dual_view 的 b 面排除注释）。
        self.assertNotIn("b", r["per_view"]["side"]["faces"])
        self.assertIn("b", r["per_view"]["front"]["faces"])

    def test_unmatched_both_views_is_fp(self):
        """杆在 front 与 side 投影都存在且都未匹配 → FP 一次。"""
        m = _model([("bar_far", "n1", "n4", "f")])  # n1→n4 远离全部 GT 投影
        r = eval_a2_dual_view(_gt(), m, tols=[500.0])
        full = r["calibers"]["full"]
        self.assertEqual(full["sweep"][0]["tp"], 0)
        self.assertEqual(full["sweep"][0]["fp"], 1)


if __name__ == "__main__":
    unittest.main()
