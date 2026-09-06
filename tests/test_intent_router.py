# -*- coding: utf-8 -*-
"""Phase 2c 意图路由单元测试（intent_router + view_regions 单点接线）。

覆盖（不依赖网络/MLM——用真实 classify_batch_intents 几何路径 + tmp DXF）：
    * overlay 显式声明优先（注册后 committed overlay 语义不变）；
    * 剥离 kind/axes 的声明（Phase 2e 副本）：意图补挂 kind/axes，
      几何字段（region/origin/scale/z_offset）逐字段继承；
    * 无声明 stem：聚类合成（孪生 front/side 判据、detail/plan 单区）；
    * 未注册时 view_regions 保持原语义（B6 兜底路径不受影响）；
    * 幂等注册 / 审计报告 / 注册表清理。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from traceability.intake.intent_router import (  # noqa: E402
    _synth_from_clusters,
    _synth_from_declared,
    clear_registrations,
    intent_regions_for_stem,
    register_sheet_intents,
    registration_report,
)
from traceability.intake.sheet_intent import (  # noqa: E402
    INTENT_ASSEMBLY_FRONT,
    INTENT_ASSEMBLY_SIDE,
    INTENT_FABRICATION_DETAIL,
    INTENT_PLAN_PROJECTION,
    SheetIntent,
)
from traceability.intake.tower_spec import (  # noqa: E402
    cross_file_merge_stems,
    sheet_is_spatial_mergeable,
    view_regions,
)


def _make_dxf(tmp_path: Path, name: str, entities) -> Path:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for kind, kw in entities:
        if kind == "line":
            msp.add_line(kw["start"], kw["end"])
        elif kind == "text":
            msp.add_text(kw["text"], dxfattribs={
                "insert": kw.get("insert", (0, 0))})
        else:
            raise ValueError(kind)
    p = tmp_path / f"{name}.dxf"
    doc.saveas(str(p))
    return p


def _tower_entities(x0: float, w: float = 150.0, h: float = 300.0,
                    cols: int = 3) -> list:
    """细高格构塔：cols 列主材 + 横杆层 + 短碎填充（双线角钢指纹）。

    aspect=h/w=2.0（<2.5 立面上限）+ 11 层横杆节拍（≥3），
    几何兜底判据（use_mllm=False）能独立判出 elevation。
    """
    ents = []
    for c in range(cols):
        cx = x0 + c * w / (cols - 1)
        ents.append(("line", {"start": (cx, 0), "end": (cx, h)}))
    for y in range(0, int(h) + 1, 25):
        ents.append(("line", {"start": (x0, y), "end": (x0 + w, y)}))
    for i in range(60):  # 短碎线（斜腹杆模拟）
        ents.append(("line", {"start": (x0 + i % cols * 50, i % 250),
                              "end": (x0 + i % cols * 50 + 8, i % 250 + 8)}))
    return ents


def _si(intent: str, comps: list, conf: float = 0.9) -> SheetIntent:
    return SheetIntent(
        stem="X", intent=intent, confidence=conf, reason="test",
        features={"components": comps})


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registrations()
    yield
    clear_registrations()


class TestSynthFromDeclared:
    """剥离 kind/axes 的声明：意图补挂，几何逐字段继承。"""

    DECL = [
        {"origin": [100.0, 200.0], "region": [100, 250, 200, 500],
         "scale_x": 20.0, "scale_y": 20.0, "z_offset": 19131.0,
         "z_span_mm": 7732.0, "z_axis_up": True},
        {"origin": [300.0, 200.0], "region": [300, 450, 200, 500],
         "scale_x": 20.0, "scale_y": 20.0, "z_offset": 19131.0},
    ]

    def test_elevation_first_front_second_side(self):
        si = _si(INTENT_ASSEMBLY_SIDE, [])
        regs = _synth_from_declared(self.DECL, si)
        assert [r["kind"] for r in regs] == ["front", "side"]
        assert regs[0]["axes"] == ["x", "z"]
        assert regs[1]["axes"] == ["x", "z"]
        # 几何继承：region/origin/scale/z_offset 原样保留
        assert regs[0]["region"] == self.DECL[0]["region"]
        assert regs[0]["origin"] == self.DECL[0]["origin"]
        assert regs[0]["scale_x"] == 20.0
        assert regs[0]["z_offset"] == 19131.0
        assert regs[0]["z_axis_up"] is True
        assert regs[1]["region"] == self.DECL[1]["region"]

    def test_third_region_conservative_detail(self):
        decl = self.DECL + [{"region": [500, 600, 200, 400]}]
        regs = _synth_from_declared(decl, _si(INTENT_ASSEMBLY_SIDE, []))
        assert [r["kind"] for r in regs] == ["front", "side", "detail"]
        assert regs[2]["axes"] == []

    def test_detail_intent_all_detail(self):
        regs = _synth_from_declared(self.DECL, _si(INTENT_FABRICATION_DETAIL, []))
        assert [r["kind"] for r in regs] == ["detail", "detail"]
        assert all(r["axes"] == [] for r in regs)

    def test_plan_intent_first_plan(self):
        regs = _synth_from_declared(self.DECL, _si(INTENT_PLAN_PROJECTION, []))
        assert regs[0]["kind"] == "plan"
        assert regs[0]["axes"] == ["x", "y"]
        assert regs[1]["kind"] == "detail"

    def test_no_z_pollution_from_synth(self):
        """合成路径绝不产 z（塔级路由保持人工通道）。"""
        regs = _synth_from_declared(
            [{"region": [0, 10, 0, 10]}], _si(INTENT_ASSEMBLY_FRONT, []))
        assert "z_offset" not in regs[0] or regs[0].get("z_offset") is None


class TestSynthFromClusters:
    """无声明 stem：聚类合成。"""

    def _comps(self):
        return [
            {"n": 500, "w": 120, "h": 300, "aspect": 2.5, "h_beats": 8,
             "bbox": [0.0, 120.0, 0.0, 300.0]},
            {"n": 450, "w": 125, "h": 299, "aspect": 2.4, "h_beats": 8,
             "bbox": [200.0, 325.0, 0.0, 299.0]},
            {"n": 30, "w": 40, "h": 40, "aspect": 1.0, "h_beats": 2,
             "bbox": [500.0, 540.0, 50.0, 90.0]},
        ]

    def test_twin_clusters_side_synthesized(self):
        regs = _synth_from_clusters(_si(INTENT_ASSEMBLY_SIDE, self._comps()))
        assert [r["kind"] for r in regs] == ["front", "side"]
        assert regs[0]["region"] == [0.0, 120.0, 0.0, 300.0]
        assert regs[1]["region"] == [200.0, 325.0, 0.0, 299.0]

    def test_height_mismatch_no_side(self):
        """塔段+右侧大样（高度差 26%——JC1-07 版式）不合成 side。"""
        comps = self._comps()
        comps[1]["h"] = 222.0
        comps[1]["bbox"] = [200.0, 325.0, 0.0, 222.0]
        regs = _synth_from_clusters(_si(INTENT_ASSEMBLY_SIDE, comps))
        assert [r["kind"] for r in regs] == ["front"]

    def test_front_intent_single_front(self):
        regs = _synth_from_clusters(_si(INTENT_ASSEMBLY_FRONT, self._comps()))
        # front 意图：即使孪生簇也不拆 side（只有 side 意图才补孪生）
        assert [r["kind"] for r in regs] == ["front"]

    def test_detail_intent_single_detail(self):
        regs = _synth_from_clusters(_si(INTENT_FABRICATION_DETAIL, self._comps()))
        assert [r["kind"] for r in regs] == ["detail"]
        assert regs[0]["axes"] == []

    def test_plan_intent_single_plan(self):
        regs = _synth_from_clusters(_si(INTENT_PLAN_PROJECTION, self._comps()))
        assert [r["kind"] for r in regs] == ["plan"]
        assert regs[0]["axes"] == ["x", "y"]


class TestRegistrationAndWiring:
    """注册 + view_regions 单点接线（真实 classify 路径，MLM 走缓存/兜底）。"""

    def test_unregistered_returns_declared(self, tmp_path):
        """未注册：view_regions 返回 overlay 原声明（B6 兜底不受影响）。"""
        ov = {"view_regions": {"X-02": [
            {"kind": "front", "region": [0, 10, 0, 10], "axes": ["x", "z"]}]}}
        regs = view_regions("X-02", overlay=ov)
        assert len(regs) == 1 and regs[0]["kind"] == "front"
        assert intent_regions_for_stem("X-02", ov) == []

    def test_explicit_declaration_wins_over_registration(self, tmp_path):
        """注册后：overlay 显式声明（带 kind/axes）优先，意图不干预。"""
        dxf = _make_dxf(tmp_path, "X-02", _tower_entities(0))
        declared = [{"kind": "front", "region": [0, 999, 0, 999],
                     "axes": ["x", "z"], "origin": [1, 1]}]
        ov = {"view_regions": {"X-02": declared}}
        register_sheet_intents([dxf], ov, use_mllm=False)
        regs = view_regions("X-02", overlay=ov)
        assert regs == declared  # 逐字节不变

    def test_stripped_declaration_intent_refill(self, tmp_path):
        """剥离 kind/axes（Phase 2e 副本）：意图补挂，几何继承。"""
        dxf = _make_dxf(tmp_path, "X-02", _tower_entities(0))
        base = {"origin": [0.0, 0.0], "region": [0.0, 150.0, 0.0, 300.0],
                "scale_x": 20.0, "scale_y": 20.0, "z_offset": 5000.0}
        ov = {"view_regions": {"X-02": [dict(base)]}}
        register_sheet_intents([dxf], ov, use_mllm=False)
        regs = view_regions("X-02", overlay=ov)
        assert len(regs) == 1
        assert regs[0]["kind"] == "front"  # 塔特征 → elevation
        assert regs[0]["axes"] == ["x", "z"]
        # 几何逐字段继承
        for k, v in base.items():
            assert regs[0][k] == v
        assert sheet_is_spatial_mergeable("X-02", overlay=ov) is True

    def test_undeclared_stem_cluster_synth(self, tmp_path):
        """无声明 stem：聚类合成 front（第三梯队通用化路径）。"""
        dxf = _make_dxf(tmp_path, "Y-05", _tower_entities(0))
        ov = {"view_regions": {}}
        register_sheet_intents([dxf], ov, use_mllm=False)
        regs = view_regions("Y-05", overlay=ov)
        assert len(regs) >= 1
        assert regs[0]["kind"] == "front"
        assert regs[0]["axes"] == ["x", "z"]
        assert "z_offset" not in regs[0]

    def test_registration_idempotent_and_report(self, tmp_path):
        dxf = _make_dxf(tmp_path, "X-02", _tower_entities(0))
        ov = {}
        r1 = register_sheet_intents([dxf], ov, use_mllm=False)
        r2 = register_sheet_intents([dxf], ov, use_mllm=False)
        assert r1 is r2  # 幂等（同签名直接返回缓存对象）
        rep = registration_report(ov)
        assert rep["registered"] is True
        assert rep["n_sheets"] == 1
        assert "X-02" in rep["intents"]

    def test_report_unregistered(self):
        assert registration_report({"nope": 1})["registered"] is False

    def test_registration_failure_no_crash(self, tmp_path, monkeypatch):
        """分类失败：register 抛错由入口捕获（这里验证异常可被捕获）。"""
        import traceability.intake.intent_router as ir

        def boom(*a, **k):
            raise RuntimeError("mlm down")

        monkeypatch.setattr(ir, "classify_batch_intents", boom)
        dxf = _make_dxf(tmp_path, "X-02", _tower_entities(0))
        with pytest.raises(RuntimeError):
            register_sheet_intents([dxf], {}, use_mllm=False)
        # 注册表未污染：view_regions 回退原语义
        assert view_regions("X-02", overlay={}) == []


_JC1_OVERLAY_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples/external/guowang_35A1/layer_overlay.json")
_ZC1_OVERLAY_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples/external/guowang_35A2_zc1/layer_overlay.json")

_OVERLAYS: dict = {}


def _cached_overlay(p: Path):
    if p not in _OVERLAYS:
        if not p.exists():
            pytest.skip(f"overlay 缺失: {p}")
        _OVERLAYS[p] = json.loads(p.read_text(encoding="utf-8"))
    return _OVERLAYS[p]


class TestRealOverlaysUnchanged:
    """committed overlay（JC1/ZC1）注册前后语义不变——红线零风险。"""

    def test_declared_stems_byte_identical(self):
        for ov in (_cached_overlay(_JC1_OVERLAY_PATH),
                   _cached_overlay(_ZC1_OVERLAY_PATH)):
            for stem, want in (ov.get("view_regions") or {}).items():
                got = view_regions(stem, overlay=ov)
                assert json.dumps(got, sort_keys=True) == json.dumps(
                    want, sort_keys=True), stem

    def test_merge_stems_unchanged(self):
        want_jc1 = {"35A1-JC1-02", "35A1-JC1-04", "35A1-JC1-05",
                    "35A1-JC1-06", "35A1-JC1-07", "35C2-SJG1-ML"}
        want_zc1 = {"35A2-ZC1-05", "35A2-ZC1-07", "35A2-ZC1-08",
                    "35A2-ZC1-09", "35A2-ZC1-10", "35A2-ZC1-12"}
        assert cross_file_merge_stems(
            _cached_overlay(_JC1_OVERLAY_PATH)) == want_jc1
        assert cross_file_merge_stems(
            _cached_overlay(_ZC1_OVERLAY_PATH)) == want_zc1
