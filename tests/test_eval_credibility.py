"""P0 评测可信度批次测试（2026-08-31 审查闭环后落地）。

覆盖：
  * P0.2：evaluate_ground_truth CLI 的 --tol / --tols 解析与生效；
  * P0.3：COST_SEMANTICS 常量与 segment_cost 的 d1+d2 和语义；
  * P0.5：count_unscorable_bars 分类（缺节点引用/坐标/语义/退化）；
  * P0.6：eval_binding 绑定字段存在性（通过 subprocess 跑真 CLI 太重，
    此处只测 CLI 参数解析函数化逻辑——main 内联，改用端到端最小模型）。
"""

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (
    COST_SEMANTICS,
    count_unscorable_bars,
    segment_cost,
    segment_gates,
)


def _mini_gt():
    return {
        "nodes": {
            "g1": (2000.0, 2000.0, 10000.0),
            "g2": (0.0, 2000.0, 14000.0),
        },
        "bars": [
            {"id": "GT_1", "from": "g1", "to": "g2", "section": "L100x7"},
        ],
    }


def _mini_model():
    comps = {
        "n1": {"kind": "tower_node", "properties": {"x": 2000.0, "y": 2000.0, "z": 10000.0}},
        "n2": {"kind": "tower_node", "properties": {"x": 0.0, "y": 2000.0, "z": 14000.0}},
        "b1": {"kind": "tower_bar", "properties": {
            "from_node": "n1", "to_node": "n2", "face": "f", "role": "DIAG",
            "geometry_class": "recognized", "geometry_origin": "dxf_geom",
            "evidence_status": "recognized", "source_file": "S1",
        }},
    }
    return {"components": comps}


class TestCostSemantics(unittest.TestCase):
    """P0.3：代价语义 = d1+d2（和），报告引用 COST_SEMANTICS。"""

    def test_sum_not_max(self):
        # 两端各偏 300mm（和 600mm）：max 语义 600>500 但 <500? 不——
        # max=300<500；sum=600>500。tol=500 下 sum 语义不匹配。
        a = (0.0, 0.0, 0.0, 4000.0)
        b = (300.0, 300.0, 300.0, 3700.0)  # 每端 √(300²+300²)≈424 → sum≈849
        gates = segment_gates(a, b)
        self.assertGreater(gates["endpoint_error_mm"], math.hypot(300, 300))
        self.assertAlmostEqual(
            gates["endpoint_error_mm"],
            2 * math.hypot(300, 300), delta=2 * math.hypot(300, 300) * 1e-9 + 1e-6,
        )

    def test_constant_declares_sum(self):
        self.assertIn("d1+d2", COST_SEMANTICS)
        self.assertIn("endpoint_sum", COST_SEMANTICS)

    def test_sum_semantics_stricter_than_max(self):
        # segment_cost 返回原始和：每端 300 → 600（若语义是 max 则 300）。
        # 「tol=500 不匹配」由 hungarian_match 的 max_cost 门禁实现
        #（cost >= max_cost 视为不匹配），这里验证两层。
        from traceability.eval.metrics import hungarian_match
        a = (0.0, 0.0, 0.0, 4000.0)
        b1 = (300.0, 0.0, 300.0, 4000.0)   # 每端 300 → sum 600
        self.assertEqual(segment_cost(a, b1), 600.0)  # 和语义（非 max=300）
        matched, un_gt, un_m = hungarian_match([a], [b1], segment_cost, 500.0)
        self.assertEqual(matched, [])      # sum 600 ≥ 500 → 不匹配
        b2 = (200.0, 0.0, 200.0, 4000.0)   # 每端 200 → sum 400 < 500
        matched2, _, _ = hungarian_match([a], [b2], segment_cost, 500.0)
        self.assertEqual(len(matched2), 1) # 和 400 < 500 → 匹配


class TestUnscorableBars(unittest.TestCase):
    """P0.5：评测静默跳过的杆件分类统计。"""

    def test_categories(self):
        m = _mini_model()
        comps = m["components"]
        # 缺节点引用
        comps["b_missing_ref"] = {"kind": "tower_bar", "properties": {
            "from_node": "nX", "to_node": "n2", "face": "f", "role": "DIAG",
            "geometry_class": "recognized", "evidence_status": "recognized",
        }}
        # 缺坐标（z=None）
        comps["n3"] = {"kind": "tower_node", "properties": {"x": 0.0, "y": 0.0, "z": None}}
        comps["b_missing_coord"] = {"kind": "tower_bar", "properties": {
            "from_node": "n3", "to_node": "n2", "face": "f", "role": "DIAG",
            "geometry_class": "recognized", "evidence_status": "recognized",
        }}
        # 缺语义
        comps["b_missing_sem"] = {"kind": "tower_bar", "properties": {
            "from_node": "n1", "to_node": "n2", "face": "f", "role": "DIAG",
        }}
        # 退化（两端点相同）
        comps["b_degenerate"] = {"kind": "tower_bar", "properties": {
            "from_node": "n1", "to_node": "n1", "face": "f", "role": "DIAG",
            "geometry_class": "recognized", "evidence_status": "recognized",
        }}
        r = count_unscorable_bars(m)
        self.assertEqual(r["n_unscorable"], 4)
        self.assertEqual(r["by_reason"]["missing_node_ref"], 1)
        self.assertEqual(r["by_reason"]["missing_coordinate"], 1)
        self.assertEqual(r["by_reason"]["missing_semantics"], 1)
        self.assertEqual(r["by_reason"]["degenerate"], 1)

    def test_clean_model_zero(self):
        self.assertEqual(count_unscorable_bars(_mini_model())["n_unscorable"], 0)


class TestTolCLI(unittest.TestCase):
    """P0.2：--tol / --tols 端到端生效（最小模型跑真 CLI）。"""

    def _run(self, *extra):
        with tempfile.TemporaryDirectory() as td:
            gt_p = Path(td) / "gt.json"
            m_p = Path(td) / "model.json"
            gt_p.write_text(json.dumps(_mini_gt()), encoding="utf-8")
            m_p.write_text(json.dumps(_mini_model()), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(REPO / "scripts/evaluate_ground_truth.py"),
                 str(gt_p), str(m_p), *extra],
                capture_output=True, text=True, timeout=120,
            )
            return proc

    def test_single_tol(self):
        proc = self._run("--tol", "500")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tols=[500.0]", proc.stdout)
        # sweep 只有一档：不应出现 50/100/200 档
        self.assertNotIn("50 ", proc.stdout.split("五层口径")[0])

    def test_tols_list(self):
        proc = self._run("--tols", "100,500")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tols=[100.0, 500.0]", proc.stdout)

    def test_bad_tols_rejected(self):
        proc = self._run("--tols", "abc")
        self.assertEqual(proc.returncode, 2)

    def test_binding_header(self):
        proc = self._run("--tol", "500")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cost=d1+d2", proc.stdout)
        self.assertIn("dataset_split=development", proc.stdout)
        self.assertIn("gt=", proc.stdout)


if __name__ == "__main__":
    unittest.main()
