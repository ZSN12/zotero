#!/usr/bin/env python3
"""Phase 3 单测：repair_dangling_endpoints（悬空断裂修复）。

覆盖四类行为：
    1. 微型残段清除（<250mm、有真悬空端、非 CROSS/corner/diaphragm）
    2. 端点焊接（<=350mm 内最近有效节点，端点引用重指）
    3. 安全豁免：CROSS 横担端头 / T 形接头 / 径向悬臂端不碰
    4. 伙伴杆缺失（无可接结构）：不修，留给 review_queue
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traceability.solve.tower_geometry import (
    inspect_model_topology,
    repair_dangling_endpoints,
)


def _mk(item_bars):
    """bars: [(id, from, to, extra)] -> List[dict]"""
    blist = []
    for item in item_bars:
        bid, f, t = item[0], item[1], item[2]
        extra = item[3] if len(item) > 3 else {}
        b = {"id": bid, "from": f, "to": t}
        b.update(extra)
        blist.append(b)
    return blist


class TestStubRemoval(unittest.TestCase):
    def test_isolated_short_stub_removed(self):
        # 160mm 孤立短杆（两端 deg=1）应被删除
        nodes = {
            "A": (0.0, 0.0, 0.0),
            "B": (0.0, 0.0, 160.0),
            "C": (1000.0, 0.0, 0.0),
            "D": (1000.0, 0.0, 1600.0),
        }
        bars = [
            ("stub", "A", "B", {"role": "HORIZ"}),
            ("diag", "C", "D", {"role": "DIAG"}),
        ]
        out, rep = repair_dangling_endpoints(nodes, _mk(bars))
        ids = [b["id"] for b in out]
        self.assertNotIn("stub", ids)
        self.assertIn("diag", ids)
        self.assertEqual(rep["removed_stub_bars"], ["stub"])

    def test_long_isolated_bar_kept(self):
        # 950mm 孤立长杆不是残段，保留（只能焊接或 review）
        nodes = {"A": (0.0, 0.0, 0.0), "B": (0.0, 0.0, 950.0)}
        bars = [("diag", "A", "B", {"role": "DIAG"})]
        out, rep = repair_dangling_endpoints(nodes, _mk(bars))
        self.assertEqual(len(out), 1)
        self.assertEqual(rep["removed_stub_bars"], [])

    def test_crossarm_stub_kept(self):
        # CROSS 角色的短杆（横担悬臂）不删
        nodes = {"A": (0.0, 0.0, 0.0), "B": (0.0, 200.0, 0.0)}
        bars = [("cross", "A", "B", {"role": "CROSS"})]
        out, _ = repair_dangling_endpoints(nodes, _mk(bars))
        self.assertEqual(len(out), 1)

    def test_tjunction_stub_kept(self):
        # 短杆端点落在另一根杆身上（T 形接头）：已物理连接，不删
        nodes = {
            "A": (0.0, 0.0, 75.0),    # 恰在 host 杆身上 -> T 形接头
            "B": (0.0, 0.0, 225.0),
            "C": (-500.0, 0.0, 75.0),
            "D": (500.0, 0.0, 75.0),
        }
        bars = [
            ("host", "C", "D", {"role": "HORIZ"}),
            ("stub_on_host", "A", "B", {"role": "DIAG"}),
        ]
        out, rep = repair_dangling_endpoints(nodes, _mk(bars))
        ids = [b["id"] for b in out]
        self.assertIn("stub_on_host", ids)
        self.assertEqual(rep["removed_stub_bars"], [])


class TestWeld(unittest.TestCase):
    def test_weld_to_nearby_joint_node(self):
        # 斜材断裂端点 200mm 内有度 >=1 节点：焊接（引用重指）
        nodes = {
            "A": (0.0, 0.0, 0.0),
            "B": (0.0, 200.0, 1000.0),   # 斜材断裂端
            "J": (0.0, 0.0, 1000.0),     # 关节节点（有横杆引用）
            "K": (1000.0, 0.0, 1000.0),
        }
        bars = [
            ("diag", "A", "B", {"role": "DIAG"}),
            ("horiz", "J", "K", {"role": "HORIZ"}),
        ]
        out, rep = repair_dangling_endpoints(nodes, _mk(bars))
        diag = next(b for b in out if b["id"] == "diag")
        self.assertEqual(diag["to"], "J")
        self.assertEqual(len(rep["welded"]), 1)
        self.assertEqual(rep["welded"][0]["welded_to"], "J")

    def test_no_weld_beyond_radius(self):
        # 最近节点 500mm（>350mm）：不焊接，留给 review
        nodes = {
            "A": (0.0, 0.0, 0.0),
            "B": (0.0, 500.0, 1000.0),
            "J": (0.0, 0.0, 1000.0),
            "K": (1000.0, 0.0, 1000.0),
        }
        bars = [
            ("diag", "A", "B", {"role": "DIAG"}),
            ("horiz", "J", "K", {"role": "HORIZ"}),
        ]
        out, rep = repair_dangling_endpoints(nodes, _mk(bars))
        diag = next(b for b in out if b["id"] == "diag")
        self.assertEqual(diag["to"], "B")
        self.assertEqual(rep["welded"], [])

    def test_crossarm_radial_tip_not_welded(self):
        # 径向远超塔身半宽（>1.4x）的悬臂端头不焊接
        def hw(z):
            return 500.0

        nodes = {
            "A": (0.0, 0.0, 0.0),
            "B": (900.0, 0.0, 1000.0),   # radial 900 > 500*1.4
            "J": (0.0, 0.0, 1000.0),
            "K": (0.0, 400.0, 1000.0),
        }
        bars = [
            ("brace", "A", "B", {"role": "DIAG"}),
            ("horiz", "J", "K", {"role": "HORIZ"}),
        ]
        out, rep = repair_dangling_endpoints(nodes, _mk(bars), half_width_fn=hw)
        brace = next(b for b in out if b["id"] == "brace")
        self.assertEqual(brace["to"], "B")
        self.assertEqual(rep["welded"], [])


class TestPhysicalDedup(unittest.TestCase):
    def test_four_face_mirrors_count_once(self):
        # 同一物理断裂的 4 面镜像（_F/_B/_L/_R 尾缀）应去重为 1 处
        nodes = {}
        bars = []
        for i, face in enumerate("FBLR"):
            a = f"A_{face}"
            b = f"B_{face}"
            nodes[a] = (100.0 + i * 3000.0, 0.0, 0.0)
            nodes[b] = (100.0 + i * 3000.0, 0.0, 950.0)
            bars.append((f"bar_1_front_{face}", a, b, {"role": "DIAG"}))
        topo = inspect_model_topology(nodes, _mk(bars))
        # 4 根镜像杆 × 2 个悬空端 = 8 个面实例，去重后 1 处物理断裂
        self.assertEqual(topo["genuine_dangling_degree1"], 8)
        self.assertEqual(topo["genuine_dangling_physical"], 1)

    def test_distinct_bars_count_separately(self):
        nodes = {
            "A1": (0.0, 0.0, 0.0), "B1": (0.0, 0.0, 950.0),
            "A2": (5000.0, 0.0, 0.0), "B2": (5000.0, 0.0, 950.0),
        }
        bars = [
            ("bar_1_front_F", "A1", "B1", {"role": "DIAG"}),
            ("bar_2_front_F", "A2", "B2", {"role": "DIAG"}),
        ]
        topo = inspect_model_topology(nodes, _mk(bars))
        # 2 根杆 × 2 个悬空端 = 4 个实例，2 处物理断裂
        self.assertEqual(topo["genuine_dangling_degree1"], 4)
        self.assertEqual(topo["genuine_dangling_physical"], 2)


if __name__ == "__main__":
    unittest.main()
