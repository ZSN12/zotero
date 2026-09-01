# -*- coding: utf-8 -*-
"""阶段 5.6 悬空断裂收尾的单元测试：焊接 + 残余剪除。

覆盖（对照 dbd2d13 产物实测的 45 处物理悬空 stem 分类）：
- weld 路径：自由端投影到异杆线段（制图惯例缺口 52~199mm）；
- merge 路径：投影落点贴既有节点 → 改指节点并删除悬空节点；
- 距离上限：> max_gap_mm 的端点不动；
- CROSS 横担悬臂端头豁免；
- prune：孤立短残片剪除 / 可焊接的不剪 / 长杆保留 / 件号保全 / 链式残片。
"""
import math
import unittest

from traceability.solve.tower_geometry import (
    prune_residual_dangling_bars,
    weld_dangling_endpoints_to_segments,
)


def _deg(bars, nid):
    n = 0
    for b in bars:
        n += (b["from"] == nid) + (b["to"] == nid)
    return n


class TestWeldDanglingEndpoints(unittest.TestCase):

    def test_weld_moves_endpoint_onto_segment(self):
        # 斜杆自由端 (0,0,1000) 距水平杆线段（z=900）100mm → 焊接后落投影点
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 1000.0),
            "c": (-2000.0, 0.0, 900.0),
            "d": (2000.0, 0.0, 900.0),
        }
        bars = [
            {"id": "diag", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = weld_dangling_endpoints_to_segments(nodes, bars)
        self.assertEqual(rep["welded"] + rep["merged"], 1)
        self.assertAlmostEqual(nn["b"][2], 900.0, delta=1.0)
        self.assertAlmostEqual(nn["b"][0], 0.0, delta=1.0)
        # 悬空端到目标线段距离清零（门禁 T 形接头豁免）
        self.assertEqual(_deg(nb, "b"), 1)

    def test_merge_into_existing_node(self):
        # 投影落点恰在既有节点上 → merge 路径
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 1000.0),
            "c": (-2000.0, 0.0, 950.0),
            "d": (0.0, 0.0, 950.0),   # 既有节点恰在投影点
            "e": (2000.0, 0.0, 950.0),
        }
        bars = [
            {"id": "diag", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "horiz", "from": "c", "to": "e", "role": "HORIZ"},
            {"id": "stub", "from": "d", "to": "c", "role": "HORIZ"},
        ]
        nn, nb, rep = weld_dangling_endpoints_to_segments(nodes, bars)
        self.assertEqual(rep["merged"], 1)
        diag = next(b for b in nb if b["id"] == "diag")
        self.assertIn("d", (diag["from"], diag["to"]))
        self.assertNotIn("b", nn)  # 悬空节点已删除

    def test_far_endpoint_untouched(self):
        # 300mm > 250mm 上限 → 不动
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 1200.0),
            "c": (-2000.0, 0.0, 900.0),
            "d": (2000.0, 0.0, 900.0),
        }
        bars = [
            {"id": "diag", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = weld_dangling_endpoints_to_segments(nodes, bars)
        self.assertEqual(rep["welded"] + rep["merged"], 0)
        self.assertAlmostEqual(nn["b"][2], 1200.0, delta=0.001)

    def test_crossarm_excluded(self):
        # CROSS 横担悬臂端头不处理（190 个属正常）
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 1000.0),
            "c": (-2000.0, 0.0, 900.0),
            "d": (2000.0, 0.0, 900.0),
        }
        bars = [
            {"id": "arm", "from": "a", "to": "b", "role": "CROSS"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = weld_dangling_endpoints_to_segments(nodes, bars)
        self.assertEqual(rep["welded"] + rep["merged"], 0)
        self.assertAlmostEqual(nn["b"][2], 1000.0, delta=0.001)

    def test_idempotent(self):
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 1000.0),
            "c": (-2000.0, 0.0, 900.0),
            "d": (2000.0, 0.0, 900.0),
        }
        bars = [
            {"id": "diag", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn1, nb1, rep1 = weld_dangling_endpoints_to_segments(nodes, bars)
        nn2, nb2, rep2 = weld_dangling_endpoints_to_segments(nn1, nb1)
        self.assertEqual(rep2["welded"] + rep2["merged"], 0)
        self.assertAlmostEqual(nn2["b"][2], nn1["b"][2], delta=0.001)

    def test_degenerate_weld_prunes_bar(self):
        # 退化防护：悬空端投影后与另一端塌缩（新杆长 < 150）→ 剪除该杆，
        # 不制造零长杆（2026-09-02 GLB 导出 8 根跳过实测：bar_2_front_71）
        nodes = {
            "a": (0.0, 0.0, 880.0),     # 另一端离投影点仅 20mm
            "b": (0.0, 0.0, 1000.0),    # 悬空端，投影到 z=900 横杆
            "c": (-2000.0, 0.0, 900.0),
            "d": (2000.0, 0.0, 900.0),
        }
        bars = [
            {"id": "frag", "from": "a", "to": "b", "role": "DIAG",
             "bar_id": "2"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = weld_dangling_endpoints_to_segments(nodes, bars)
        self.assertEqual(rep["degenerate_pruned"], 1)
        self.assertEqual([b["id"] for b in nb], ["horiz"])
        self.assertIn("2", rep["pruned_label_ids"])
        # 残留节点由 prune 通道收尾（weld 不做全量孤立节点清扫）
        self.assertIn("b", nn)



class TestPruneResidualDangling(unittest.TestCase):

    def test_prune_isolated_short(self):
        # 孤立短杆（两端都远离一切）→ 剪除
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 800.0),
            "c": (-2000.0, 5000.0, 900.0),
            "d": (2000.0, 5000.0, 900.0),
        }
        bars = [
            {"id": "frag", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = prune_residual_dangling_bars(nodes, bars)
        self.assertEqual(rep["pruned_bars"], 1)
        self.assertEqual([b["id"] for b in nb], ["horiz"])
        self.assertNotIn("a", nn)
        self.assertNotIn("b", nn)

    def test_weldable_not_pruned(self):
        # 自由端距线段 100mm（<=250 可焊接）→ 不剪（留给焊接通道）
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 1000.0),
            "e": (0.0, 0.0, -2000.0),
            "c": (-2000.0, 0.0, 900.0),
            "d": (2000.0, 0.0, 900.0),
        }
        bars = [
            {"id": "diag", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "leg", "from": "e", "to": "a", "role": "LEG"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = prune_residual_dangling_bars(nodes, bars)
        self.assertEqual(rep["pruned_bars"], 0)
        self.assertEqual(len(nb), 3)

    def test_long_bar_kept(self):
        # L=2000 > max_len=1800 → 保留（真实断裂需缝合）
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 2000.0),
            "c": (-2000.0, 5000.0, 900.0),
            "d": (2000.0, 5000.0, 900.0),
        }
        bars = [
            {"id": "long", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = prune_residual_dangling_bars(nodes, bars)
        self.assertEqual(rep["pruned_bars"], 0)
        self.assertEqual(len(nb), 2)

    def test_label_preserved(self):
        # 带 bar_id 的剪除杆 → 件号进 pruned_label_ids（A1 证据不丢）
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 800.0),
            "c": (-2000.0, 5000.0, 900.0),
            "d": (2000.0, 5000.0, 900.0),
        }
        bars = [
            {"id": "frag", "from": "a", "to": "b", "role": "DIAG",
             "bar_id": "318"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = prune_residual_dangling_bars(nodes, bars)
        self.assertEqual(rep["pruned_bars"], 1)
        self.assertIn("318", rep["pruned_label_ids"])

    def test_unlabeled_not_in_labels(self):
        # UNLABELED 前缀的剪除杆 → 不进件号登记
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 800.0),
            "c": (-2000.0, 5000.0, 900.0),
            "d": (2000.0, 5000.0, 900.0),
        }
        bars = [
            {"id": "frag", "from": "a", "to": "b", "role": "DIAG",
             "bar_id": "UNLABELED_CLE0011"},
            {"id": "horiz", "from": "c", "to": "d", "role": "HORIZ"},
        ]
        nn, nb, rep = prune_residual_dangling_bars(nodes, bars)
        self.assertEqual(rep["pruned_bars"], 1)
        self.assertEqual(rep["pruned_label_ids"], [])

    def test_cascade_chain(self):
        # 链式残片：f1 孤立剪除 → b 变 degree-1 → f2 第二轮剪除
        # （anchor 长杆 L=8600 > 1800 保留，c 端 degree-2 支撑链头）
        nodes = {
            "a": (0.0, 0.0, 0.0),
            "b": (0.0, 0.0, 700.0),
            "c": (0.0, 0.0, 1400.0),
            "top": (0.0, 0.0, 10000.0),
            "far": (-2000.0, 5000.0, 900.0),
            "far2": (2000.0, 5000.0, 900.0),
        }
        bars = [
            {"id": "f1", "from": "a", "to": "b", "role": "DIAG"},
            {"id": "f2", "from": "b", "to": "c", "role": "DIAG"},
            {"id": "anchor", "from": "c", "to": "top", "role": "LEG"},
            {"id": "horiz", "from": "far", "to": "far2", "role": "HORIZ"},
        ]
        nn, nb, rep = prune_residual_dangling_bars(nodes, bars)
        self.assertEqual(rep["pruned_bars"], 2)
        self.assertEqual(sorted(b["id"] for b in nb), ["anchor", "horiz"])
        self.assertEqual(rep["pruned_rounds"], 2)


if __name__ == "__main__":
    unittest.main()
