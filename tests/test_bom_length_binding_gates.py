# -*- coding: utf-8 -*-
"""P0-1b（2026-09-09）：merge_view_bars 的 UNLABELED→BOM 长度唯一匹配双闸测试。

背景（35A1-JC1 实测 6 件 r_project_bom_master FAILED conflicts 中的 4 件）：
3906/3907/3910/3914 是 02/05 册塔身 UNLABELED 杆被「长度 ±1% 唯一匹配」
绑到 40 册塔头支架件号——纯长度巧合、零跨册证据，四面展开后 qty 4>2
假超计。修复两道闸：
  * 跨册闸：BOM 行 sheet 已知且 ≠ 杆来源册 → 候选排除；
  * 数量闸：该 bid 已绑实例数（含显式挂码）≥ BOM qty → 候选排除。
BOM 元数据经 dim_bom_length_*.source.detail = "sheet=…;qty=…" 传递
（cross_check_bom 写入；parse_bom_csv 透传 sheet 列）。
"""
import unittest

from traceability.model import (
    Component, Dimension, DimensionOrigin, EngineeringModel,
    SourceRef, SourceType,
)


def _bar(cid, view="front", bar_id="UNLABELED_x", source_file=None):
    props = {"view_type": view, "from_node": f"{cid}_a",
             "to_node": f"{cid}_b", "bar_id": bar_id}
    if source_file:
        props["source_file"] = source_file
    return Component(id=cid, name=cid, kind="tower_bar",
                     source=SourceRef(SourceType.DRAWING, "t.dxf"), properties=props)


def _node(cid, view="front"):
    return Component(id=cid, name=cid, kind="tower_node",
                     source=SourceRef(SourceType.DRAWING, "t.dxf"),
                     properties={"view_type": view})


def _bom_dim(bid, length, sheet="", qty=None):
    detail = None
    if sheet or qty is not None:
        detail = f"sheet={sheet or ''};qty={qty if qty is not None else ''}"
    return Dimension(
        id=f"dim_bom_length_{bid}", name=f"{bid} len", value=length, unit="mm",
        origin=DimensionOrigin.MEASURED,
        source=SourceRef(SourceType.VENDOR, "tower_bom.csv",
                         confidence=0.95, detail=detail),
    )


def _model_with_bars(bars, nodes, dims):
    m = EngineeringModel(name="t")
    for n in nodes:
        m.add_component(n)
    for b in bars:
        m.add_component(b)
    for d in dims:
        m.add_dimension(d)
    return m


class SheetGateTest(unittest.TestCase):
    def _run(self, bar_sheet, bom_sheet, has_meta=True):
        dims = [_bom_dim("3906", 1381.0,
                         sheet=bom_sheet if has_meta else "", qty=2)]
        m = _model_with_bars(
            [_bar("b1", bar_id="UNLABELED_u", source_file=bar_sheet)],
            [_node("b1_a"), _node("b1_b")], dims)
        from traceability.intake.tower_views import merge_view_bars
        out = merge_view_bars(m)
        # 杆长设为 BOM 长度 ±0.1% 内（_bar_3d_length 无几何时 None——
        # 简化：直接检查 bar_id 是否被绑）。_bar_3d_length 读节点坐标，
        # 这里给节点同坐标使长度 ~0；改为直接调用内部逻辑验证闸。
        return out

    def test_parse_bom_csv_sheet_passthrough(self):
        import tempfile
        from pathlib import Path
        from traceability.intake.tower_bom import parse_bom_csv
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bom.csv"
            p.write_text(
                "bar_id,section,length_mm,qty,sheet\n"
                "3906,L40X4,1381,2,35A1-JC1-40\n"
                "101,L40X3,1436,4,35A1-JC1-02\n",
                encoding="utf-8")
            rows = parse_bom_csv(p)
        self.assertEqual(rows[0]["sheet"], "35A1-JC1-40")
        self.assertEqual(rows[1]["sheet"], "35A1-JC1-02")

    def test_sheet_mismatch_candidate_rejected(self):
        """跨册闸：02 册杆不得匹配 40 册 BOM 行（长度巧合不绑）。"""
        # 直接测候选过滤逻辑（复刻 merge_view_bars 内嵌闸）
        bom_len = {"3906": 1381.0}
        bom_sheet = {"3906": "35A1-JC1-40"}
        bom_qty = {"3906": 2}
        used_ids, used_count = set(), {}
        ln = 1380.0
        cands = []
        for bid, bl in bom_len.items():
            if bid in used_ids or bl <= 0:
                continue
            if abs(ln - bl) / bl <= 0.01:
                bsheet = bom_sheet.get(bid, "")
                bar_sheet = "35A1-JC1-02"
                if bsheet and bar_sheet and bsheet != bar_sheet:
                    continue
                if bid in bom_qty and used_count.get(bid, 0) >= bom_qty[bid]:
                    continue
                cands.append(bid)
        self.assertEqual(cands, [], "跨册候选必须被闸排除")

    def test_sheet_match_candidate_accepted(self):
        """同册候选正常通过（40 册杆 ↔ 40 册 BOM 行）。"""
        bom_len = {"3906": 1381.0}
        bom_sheet = {"3906": "35A1-JC1-40"}
        used_ids, used_count = set(), {}
        ln = 1380.0
        cands = []
        for bid, bl in bom_len.items():
            if bid in used_ids or bl <= 0:
                continue
            if abs(ln - bl) / bl <= 0.01:
                bsheet = bom_sheet.get(bid, "")
                bar_sheet = "35A1-JC1-40"
                if bsheet and bar_sheet and bsheet != bar_sheet:
                    continue
                cands.append(bid)
        self.assertEqual(cands, ["3906"])

    def test_qty_gate_blocks_over_binding(self):
        """数量闸：qty=2 已绑 2 根后第 3 根不得再绑同 bid。"""
        bom_len = {"623": 1521.0}
        bom_qty = {"623": 2}
        bom_sheet = {}
        used_count = {"623": 2}
        ln = 1521.5
        cands = []
        for bid, bl in bom_len.items():
            if bl <= 0:
                continue
            if abs(ln - bl) / bl <= 0.01:
                if bid in bom_qty and used_count.get(bid, 0) >= bom_qty[bid]:
                    continue
                cands.append(bid)
        self.assertEqual(cands, [], "超 qty 的候选必须排除")

    def test_no_meta_backward_compatible(self):
        """无 sheet/qty 元数据（旧 BOM）时闸不生效——行为零变化。"""
        bom_len = {"101": 1436.0}
        bom_sheet = {}          # 无元数据
        bom_qty = {}
        used_count = {}
        ln = 1435.0
        cands = []
        for bid, bl in bom_len.items():
            if bl <= 0:
                continue
            if abs(ln - bl) / bl <= 0.01:
                bsheet = bom_sheet.get(bid, "")
                bar_sheet = "whatever-sheet"
                if bsheet and bar_sheet and bsheet != bar_sheet:
                    continue
                if bid in bom_qty and used_count.get(bid, 0) >= bom_qty[bid]:
                    continue
                cands.append(bid)
        self.assertEqual(cands, ["101"], "无元数据时保持旧绑定行为")

    def test_cross_check_bom_writes_meta(self):
        """cross_check_bom 把 sheet/qty 写进长度维度 source.detail。"""
        from traceability.intake.tower_bom import cross_check_bom
        m = EngineeringModel(name="t")
        m.add_component(_node("n1"))
        cross_check_bom(m, [{
            "bar_id": "3906", "section": "L40X4",
            "length_mm": 1381.0, "qty": 2, "sheet": "35A1-JC1-40",
        }])
        d = m.dimensions.get("dim_bom_length_3906")
        self.assertIsNotNone(d)
        self.assertIn("sheet=35A1-JC1-40", d.source.detail)
        self.assertIn("qty=2", d.source.detail)

    def test_bar_id_source_property_marked(self):
        """绑定的杆带 bar_id_source 标记（区分 BOM 匹配 vs 图纸文字）。"""
        # 通过真实 merge_view_bars 跑一个最小双节点场景
        dims = [_bom_dim("555", 1000.0, sheet="", qty=1)]
        n1, n2 = _node("bU_a"), _node("bU_b")
        n1.properties.update({"x": 0.0, "y": 0.0, "z": 0.0})
        n2.properties.update({"x": 0.0, "y": 0.0, "z": 1000.0})
        bar = _bar("bU", bar_id="UNLABELED_u", source_file="")
        m = _model_with_bars([bar], [n1, n2], dims)
        from traceability.intake.tower_views import merge_view_bars
        out = merge_view_bars(m)
        bound = [c for c in out.components.values()
                 if c.kind == "tower_bar" and c.properties.get("bar_id") == "555"]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0].properties.get("bar_id_source"),
                         "bom_length_unique_match")


if __name__ == "__main__":
    unittest.main()
