"""run_batch_benchmark 批跑矩阵单测（阶段五任务一）。

覆盖：
  * 塔注册表完整性（DXF 目录 / GT 路径存在性标注）；
  * ladder_benchmark 的双指标口径（结点精度 tol300 / 层位召回 tol500）；
  * 门禁逻辑：01-1 模式看结点精度，distributed 模式看层位召回，
    任一 ≥80% 达标；
  * _parse_a1 对 evaluate_ground_truth.py stdout 的结构化抽取。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from run_batch_benchmark import (  # noqa: E402
    TOWERS,
    _parse_a1,
    ladder_benchmark,
)


class TestTowerRegistry:
    def test_four_towers_registered(self):
        assert set(TOWERS) >= {"jc1", "zc1", "szc1", "a1zc1"}

    def test_jc1_dxf_dir_exists(self):
        assert (REPO / TOWERS["jc1"]["dxf_dir"]).exists(), "JC1 批次 DXF 缺失"


class TestLadderBenchmark:
    @pytest.mark.skipif(
        not (REPO / TOWERS["jc1"]["dxf_dir"]).exists()
        or not (REPO / TOWERS["jc1"]["gt"]).exists(),
        reason="35A1-JC1 外部数据不在仓库")
    def test_jc1_node_precision_100(self):
        """JC1 主路径：01-1 阶梯结点精度应为满分（结点全中 GT 层）。"""
        row = ladder_benchmark("jc1", TOWERS["jc1"])
        assert row["ok"], row.get("error")
        assert row["mode"] == "01-1"
        assert row["body_hit_pct"] >= 80.0

    @pytest.mark.skipif(
        not (REPO / TOWERS["a1zc1"]["dxf_dir"]).exists()
        or not (REPO / TOWERS["a1zc1"]["gt"]).exists(),
        reason="35A1-ZC1 外部数据不在仓库")
    def test_a1zc1_distributed_mode_level_recall(self):
        """35A1-ZC1 分散式版式：层位召回口径（±500mm）应 ≥80%。"""
        row = ladder_benchmark("a1zc1", TOWERS["a1zc1"])
        assert row["ok"], row.get("error")
        assert row["mode"] == "distributed"
        assert row["level_recall_pct"] >= 80.0

    def test_missing_dxf_dir_reports_error(self):
        row = ladder_benchmark("x", {"name": "X", "dxf_dir": "out/nonexistent",
                                     "index_sheet": "X.dxf", "gt": "nope.json"})
        assert not row["ok"]
        assert "error" in row


class TestParseA1:
    def test_bom_line(self):
        s = ("=== A1 件号识别（独立于几何匹配）===\n"
             "图纸件号（BOM）: 202，模型识别件号: 190，"
             "Exact Match: 190（P=100.0% R=94.1%）\n")
        out = _parse_a1(s)
        assert "Exact Match: 190" in out and "P=100.0%" in out

    def test_gt_line(self):
        s = ("=== A1 件号识别 ===\n"
             "GT 件号: 1071，模型识别件号: 91，Exact Match: 0（P=0.0% R=0.0%）\n")
        out = _parse_a1(s)
        assert "Exact Match: 0" in out

    def test_missing(self):
        assert _parse_a1("nothing here") == "A1 行缺失"
