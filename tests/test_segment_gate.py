"""Phase 1 段门禁回归测试：scripts/diagnose_recall.py segment_gate。

合成小 fixture（不依赖 out/ 产物）：
    * z 窗口从指定分册杆件端点现算；窗口外 GT/模型杆被排除
    * caliber=pure 只留 recognized 层（镜像/横隔/标高辅助排除）
    * 分册号归一化（"06" ≡ "35A1-JC1-06"）
    * pass 判定与退出码语义（P/R 双阈值）
    * 无该分册杆 → error + pass=False
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "diagnose_recall", REPO / "scripts" / "diagnose_recall.py")
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)


def _gt(bars, nodes):
    return {"name": "fixture-gt", "nodes": nodes, "bars": bars}


def _mk_model(bars, nodes):
    comps = {}
    for nid, (x, y, z) in nodes.items():
        comps[nid] = {"id": nid, "kind": "tower_node", "name": nid,
                      "properties": {"x": x, "y": y, "z": z}}
    for cid, fn, tn, props in bars:
        base = {"from_node": fn, "to_node": tn, "geometry_class": "recognized",
                "geometry_origin": "dxf_geom", "face": "f", "role": "DIAG"}
        base.update(props)
        comps[cid] = {"id": cid, "kind": "tower_bar", "name": cid, "properties": base}
    return {"name": "m", "components": comps}


# 场景：06 段 z 窗口 ≈ [13000, 16000]
NODES = {"A": (0, 0, 13000), "B": (1000, 0, 13000), "C": (0, 0, 16000),
         "D": (1000, 0, 16000), "E": (0, 0, 30000), "F": (1000, 0, 30000)}
MODEL_BARS = [
    # 分册 06 的杆（定义 z 窗口）
    ("m_ok", "A", "C", {"source_file": "35A1-JC1-06"}),                       # recognized front，匹配 GT
    ("m_mirror", "B", "D", {"source_file": "35A1-JC1-06", "face": "b"}),      # 镜像 → reconstructed
    ("m_dia", "A", "B", {"source_file": "35A1-JC1-06", "face": "diaphragm",
                          "geometry_class": "reconstructed",
                          "geometry_origin": "diaphragm_reconstructed",
                          "level_source": "gt_canonical"}),                    # recognition mode 即排除
    ("m_sub", "A", "D", {"source_file": "35A1-JC1-06",
                         "level_source": "gt_canonical"}),                     # 过 recognition mode 但口径判 level_assisted
    ("m_far", "E", "F", {"source_file": "35A1-JC1-02"}),                      # 他册杆，z 窗口外
]
GT_BARS = [
    {"id": "G1", "from": "A", "to": "C", "segments": 1},                      # 与 m_ok 同线
    {"id": "G2", "from": "B", "to": "D", "segments": 1},                      # 只有镜像杆（pure 不该匹配到）
    {"id": "G3", "from": "E", "to": "F", "segments": 1},                      # 窗口外
]


class TestSegmentGate(unittest.TestCase):
    def setUp(self):
        self.gt = _gt(GT_BARS, {k: list(v) for k, v in NODES.items()})
        self.model = _mk_model(MODEL_BARS, NODES)

    def test_z_window_and_gt_subset(self):
        r = dr.segment_gate(self.gt, self.model, "06", "all", "front", 500.0, 85.0)
        self.assertEqual(r["z_window_mm"], [13000.0, 16000.0])
        self.assertEqual(r["n_gt"], 2)          # G3 窗口外被排除
        self.assertEqual(r["n_model"], 2)       # m_far/m_dia 被 mode 排除；m_sub 在（all 口径）

    def test_pure_caliber_excludes_mirror_and_assisted(self):
        r = dr.segment_gate(self.gt, self.model, "06", "pure", "front", 500.0, 85.0)
        self.assertEqual(r["n_model"], 1)       # m_sub 被五层口径判 level_assisted 剔除，只剩 m_ok
        self.assertEqual(r["tp"], 1)
        self.assertEqual(r["recall_pct"], 50.0)  # 1/2 GT
        self.assertEqual(r["precision_pct"], 100.0)

    def test_gate_pass_fail_semantics(self):
        ok = dr.segment_gate(self.gt, self.model, "06", "pure", "front", 500.0, 50.0)
        self.assertTrue(ok["pass"])             # R=50 ≥ 50, P=100 ≥ 50
        bad = dr.segment_gate(self.gt, self.model, "06", "pure", "front", 500.0, 85.0)
        self.assertFalse(bad["pass"])           # R=50 < 85

    def test_segment_code_normalization(self):
        r1 = dr.segment_gate(self.gt, self.model, "06", "pure", "front", 500.0, 85.0)
        r2 = dr.segment_gate(self.gt, self.model, "35A1-JC1-06", "pure", "front", 500.0, 85.0)
        self.assertEqual(r1["z_window_mm"], r2["z_window_mm"])

    def test_missing_segment_errors(self):
        r = dr.segment_gate(self.gt, self.model, "99", "pure", "front", 500.0, 85.0)
        self.assertIn("error", r)
        self.assertFalse(r["pass"])


if __name__ == "__main__":
    unittest.main()
