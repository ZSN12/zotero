# -*- coding: utf-8 -*-
"""sheet_intent 四分类单元测试（Phase 2b）。

覆盖确定性判据链的核心环节（不依赖网络/MLM）：
    * 文件名规则出局（图签/材料表）
    * union-find 端点吸附连通分量
    * 塔形簇跨度（显著簇 + aspect 带通）
    * 双线角钢指纹
    * classify_batch_intents 的 MLLM 判定融合（mock verdict 注入）：
      表格指纹 / 双线门 / 缩微模型门 / 立面反证
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from traceability.intake.sheet_intent import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    INTENT_ASSEMBLY_FRONT,
    INTENT_ASSEMBLY_SIDE,
    INTENT_FABRICATION_DETAIL,
    INTENT_PLAN_PROJECTION,
    INTENT_TO_SHEET_ROLE,
    SheetIntent,
    _components_of,
    _double_line_ratio,
    _filename_intent,
    _segment_cloud,
    _sheet_line_features,
    _tower_cluster_span,
    classify_batch_intents,
)


def _make_dxf(tmp_path: Path, name: str, entities) -> Path:
    """按 (type, kwargs) 列表生成一个最小 DXF。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for kind, kw in entities:
        if kind == "line":
            msp.add_line(kw["start"], kw["end"])
        elif kind == "text":
            msp.add_text(kw["text"], dxfattribs={
                "insert": kw.get("insert", (0, 0))})
        elif kind == "lwpolyline":
            msp.add_lwpolyline(kw["points"], close=kw.get("close", False))
        elif kind == "dimension":
            # DIMENSION 需要 dimstyle；测试中用 TEXT 替代计数即可，
            # 这里只构造合法实体避免 ezdxf 报错。
            msp.add_text(kw.get("text", "100"), dxfattribs={"insert": (0, 0)})
        else:
            raise ValueError(kind)
    p = tmp_path / f"{name}.dxf"
    doc.saveas(str(p))
    return p


class TestFilenameIntent:
    def test_title_block_out(self):
        fn = _filename_intent("X-00-1")
        assert fn and fn.get("out") is True
        assert fn["intent"] == INTENT_FABRICATION_DETAIL

    def test_bom_out(self):
        fn = _filename_intent("X-ML")
        assert fn and fn.get("out") is True
        assert fn["intent"] == INTENT_FABRICATION_DETAIL

    def test_elevation_not_decided_by_filename(self):
        assert _filename_intent("X-02") is None


class TestComponentsOf:
    def test_touching_segments_join(self):
        segs = [(0, 0, 10, 0), (10, 0, 10, 10), (10, 10, 0, 10)]
        comps = _components_of(segs, tol=1.0)
        assert len(comps) == 1
        assert len(next(iter(comps.values()))) == 3

    def test_far_apart_segments_split(self):
        segs = [(0, 0, 10, 0), (1000, 1000, 1010, 1000)]
        comps = _components_of(segs, tol=4.0)
        assert len(comps) == 2


class TestTowerClusterSpan:
    def test_square_detail_cluster_excluded(self):
        # 方形节点大样（aspect 1.0 但带 30 线簇阈值时不是最大簇）+
        # 细高塔簇（aspect 2.0）→ span 取塔簇跨度。
        feats = {"components": [
            {"n": 300, "w": 100.0, "h": 90.0, "aspect": 0.9, "h_beats": 5},
            {"n": 200, "w": 150.0, "h": 300.0, "aspect": 2.0, "h_beats": 4},
        ]}
        assert _tower_cluster_span(feats) == pytest.approx(300.0)

    def test_insignificant_thin_cluster_excluded(self):
        # 9 线细长条（aspect 5.9）远小于 30%×248 → 不参与。
        feats = {"components": [
            {"n": 248, "w": 96.3, "h": 85.0, "aspect": 0.882, "h_beats": 5},
            {"n": 9, "w": 120.0, "h": 296.0, "aspect": 2.467, "h_beats": 3},
        ]}
        assert _tower_cluster_span(feats) == 0.0

    def test_empty(self):
        assert _tower_cluster_span({}) == 0.0
        assert _tower_cluster_span({"components": []}) == 0.0


class TestDoubleLineRatio:
    def test_single_line_skeleton_low(self, tmp_path):
        # 单线骨架：几条长线，无短碎线。
        ents = [("line", {"start": (0, 0), "end": (0, 100)})]
        for i in range(1, 20):
            ents.append(("line", {"start": (0, i * 5), "end": (50, i * 5 + 25)}))
        ents.append(("line", {"start": (0, 100), "end": (0, 200)}))
        p = _make_dxf(tmp_path, "single_line", ents)
        # 线数 < 50 → None（指纹需要足量线）
        assert _double_line_ratio(p) is None

    def test_double_line_elevation_high(self, tmp_path):
        # 双线角钢：长杆 + 大量短碎填充线。
        ents = []
        n = 0
        for y in range(0, 100, 10):
            ents.append(("line", {"start": (0, y), "end": (0, y + 10)}))
            ents.append(("line", {"start": (2, y), "end": (2, y + 10)}))
            n += 2
        for i in range(80):
            ents.append(("line", {
                "start": (0, i), "end": (0.5, i + 0.4)}))
            n += 1
        p = _make_dxf(tmp_path, "double_line", ents)
        ratio = _double_line_ratio(p)
        assert ratio is not None
        assert ratio > 0.3


class TestSheetLineFeatures:
    def test_counts_and_components(self, tmp_path):
        ents = [
            ("line", {"start": (0, 0), "end": (0, 50)}),
            ("line", {"start": (0, 50), "end": (30, 50)}),
            ("line", {"start": (30, 50), "end": (30, 0)}),
            ("line", {"start": (0, 25), "end": (30, 25)}),
            ("text", {"text": "1234"}),
            ("text", {"text": "L50x5"}),
        ]
        p = _make_dxf(tmp_path, "basic", ents)
        f = _sheet_line_features(p)
        assert f["n_line"] == 4
        assert f["n_text"] == 2
        assert f["n_numeric_text"] == 2
        # 只有 4 线 < _MIN_COMPONENT_LINES(8)，分量列表为空但计数正确
        assert f["components"] == []


class _FakeMLM:
    """verdict 注入式 mock：per-stem 预设 MLLM 判定。"""

    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.model = "fake"
        self.provider = "fake"

    def available(self):
        return True

    def call_agent_json(self, prompt, image_path, schema, agent=None):
        stem = Path(image_path).stem.split("__")[0]
        v = self.verdicts[stem]
        return v, {"model": "fake"}


def _batch(tmp_path, name, sheets):
    """sheets: [(stem, entities)] → DXF 路径列表。"""
    paths = []
    for stem, ents in sheets:
        paths.append(_make_dxf(tmp_path, stem, ents))
    return paths


def _tall_entities(width=120, height=300):
    """aspect 带通内的塔形簇（1.0~4.5），线数充足，短碎线密集
    （双线角钢指纹）。跨径 height vs 大样 5 的悬殊比例走缩微门。"""
    ents = []
    n_cols = 3
    for c in range(n_cols):
        x = c * width // n_cols
        for y in range(0, height, 30):
            ents.append(("line", {"start": (x, y), "end": (x, y + 30)}))
    for y in range(0, height, 30):
        for c in range(n_cols - 1):
            x = c * width // n_cols
            ents.append(("line", {"start": (x, y), "end": (x + width // n_cols, y)}))
            ents.append(("line", {"start": (x, y + 15), "end": (x + width // n_cols, y + 30)}))
    for i in range(200):
        ents.append(("line", {"start": (i % width, i % height),
                              "end": (i % width + 0.4, i % height + 0.3)}))
    return ents


def _detail_entities():
    """方形小节点大样（aspect≈1，跨径小），双线角钢画法带短碎线
    （真实节点大样 dbl≈0.42，不走双线门而走缩微门）。"""
    ents = []
    for i in range(30):
        ents.append(("line", {"start": (i, 0), "end": (i + 0.5, 30)}))
    for j in range(30):
        ents.append(("line", {"start": (0, j), "end": (30, j + 0.5)}))
    for i in range(60):
        ents.append(("line", {
            "start": (i % 30, i % 30),
            "end": (i % 30 + 0.3, i % 30 + 0.2)}))
    return ents


def _bom_entities():
    """材料表版式：少量结构线 + 海量数字文本。"""
    ents = []
    for r in range(8):
        ents.append(("line", {"start": (0, r * 5), "end": (200, r * 5)}))
    for i in range(600):
        ents.append(("text", {"text": f"{i}", "insert": (i % 200, i % 40)}))
    return ents


class TestClassifyBatchFusion:
    def test_mllm_elevation_with_evidence_kept(self, tmp_path):
        paths = _batch(tmp_path, "fuse1", [
            ("T-02", _tall_entities(120, 300)),
        ])
        mllm = _FakeMLM({"T-02": {
            "intent": "assembly_elevation_front", "confidence": 0.9,
            "reason": "格构塔身"}})
        res = classify_batch_intents(paths, mllm=mllm,
                                     cache_dir=tmp_path / "c")
        si = res["T-02"]
        assert si.intent == INTENT_ASSEMBLY_FRONT
        assert si.mllm_review and si.mllm_review.get("intent") == "assembly_elevation_front"

    def test_mllm_detail_but_evidence_overrides(self, tmp_path):
        # MLLM 反判 detail，但双线指纹+塔形簇跨度+主簇线数全达标
        # → 确定性立面反证。图册参照来自同批的 T-02。
        paths = _batch(tmp_path, "fuse2", [
            ("T-02", _tall_entities(120, 300)),
            ("T-06", _tall_entities(100, 250)),
        ])
        mllm = _FakeMLM({
            "T-02": {"intent": "assembly_elevation_front", "confidence": 0.9,
                     "reason": "格构塔身"},
            "T-06": {"intent": "fabrication_detail", "confidence": 0.8,
                     "reason": "误判大样"},
        })
        res = classify_batch_intents(paths, mllm=mllm,
                                     cache_dir=tmp_path / "c")
        si = res["T-06"]
        assert si.intent == INTENT_ASSEMBLY_FRONT
        assert si.mllm_review.get("overridden_by") == "elevation_evidence"

    def test_table_signature_overrides_mllm(self, tmp_path):
        paths = _batch(tmp_path, "fuse3", [
            ("T-02", _tall_entities(120, 300)),
            ("T-ML2", _bom_entities()),
        ])
        mllm = _FakeMLM({
            "T-02": {"intent": "assembly_elevation_front", "confidence": 0.9,
                     "reason": "格构塔身"},
            "T-ML2": {"intent": "plan_projection", "confidence": 0.7,
                      "reason": "矩阵网格"},
        })
        res = classify_batch_intents(paths, mllm=mllm,
                                     cache_dir=tmp_path / "c")
        si = res["T-ML2"]
        assert si.intent == INTENT_FABRICATION_DETAIL
        assert si.mllm_review.get("overridden_by") == "table_signature"

    def test_miniature_gate_downgrades(self, tmp_path):
        # 大样页 MLLM 误判 front（多呼高缩微模型场景），
        # 但塔形簇跨度和图册参照差距悬殊 → 缩微模型门降级。
        paths = _batch(tmp_path, "fuse4", [
            ("T-02", _tall_entities(120, 300)),
            ("T-03", _detail_entities()),
        ])
        mllm = _FakeMLM({
            "T-02": {"intent": "assembly_elevation_front", "confidence": 0.9,
                     "reason": "格构塔身"},
            "T-03": {"intent": "assembly_elevation_front", "confidence": 0.8,
                     "reason": "缩微格构"},
        })
        res = classify_batch_intents(paths, mllm=mllm,
                                     cache_dir=tmp_path / "c")
        si = res["T-03"]
        assert si.intent == INTENT_FABRICATION_DETAIL
        assert si.mllm_review.get("overridden_by") == "miniature_gate"

    def test_filename_rule_skips_mllm(self, tmp_path):
        paths = _batch(tmp_path, "fuse5", [
            ("T-00-1", _bom_entities()),
        ])
        called = []

        class _Probe(_FakeMLM):
            def call_agent_json(self, prompt, image_path, schema, agent=None):
                called.append(Path(image_path).stem)
                return {"intent": "assembly_elevation_front",
                        "confidence": 0.9, "reason": "x"}, {}

        res = classify_batch_intents(
            paths, mllm=_Probe({}), cache_dir=tmp_path / "c")
        assert res["T-00-1"].intent == INTENT_FABRICATION_DETAIL
        assert res["T-00-1"].filename_rule is not None
        assert called == []  # 图签/材料表零成本出局，不经 MLLM

    def test_no_mllm_geometric_fallback(self, tmp_path):
        paths = _batch(tmp_path, "fuse6", [
            ("T-02", _tall_entities(120, 300)),
        ])
        res = classify_batch_intents(paths, use_mllm=False,
                                     cache_dir=tmp_path / "c")
        assert res["T-02"].mllm_review is None
        assert "兜底" in res["T-02"].reason


class TestIntentRoleMapping:
    def test_roles(self):
        assert INTENT_TO_SHEET_ROLE[INTENT_ASSEMBLY_FRONT] == "elevation"
        assert INTENT_TO_SHEET_ROLE[INTENT_ASSEMBLY_SIDE] == "elevation"
        assert INTENT_TO_SHEET_ROLE[INTENT_FABRICATION_DETAIL] == "node_detail"
        assert INTENT_TO_SHEET_ROLE[INTENT_PLAN_PROJECTION] == "plan"


class TestSheetIntentDataclass:
    def test_to_dict_roundtrip(self):
        si = SheetIntent(
            stem="X-02", intent=INTENT_ASSEMBLY_FRONT, confidence=0.9,
            reason="测试", features={"n_line": 10},
            filename_rule=None, mllm_review={"intent": "assembly_elevation_front"},
        )
        d = si.to_dict()
        assert json.loads(json.dumps(d, ensure_ascii=False))["stem"] == "X-02"
        assert d["confidence"] == 0.9
