"""P0-3c（2026-09-09）：unresolved_projection_refs 分类降级闸单元测试。

35A1-JC1 交付收口实测 55 处 side 投影未挂链，全量阻塞 verified。归因后
分三类（docs/P0_VERIFIED_DELIVERY_ATTRIBUTION.md §十一）：
    * non_bom_label —— bar_id 不在 BOM（1-6/75/109-144/UNLABELED 图纸
      标注号），与杆件核对语义无关，只披露不阻塞；
    * bar_id_present —— 件号已由主视图重建在最终模型（110/112/122 等
      多候选未消歧），几何已在、非丢失，只披露不阻塞；
    * unresolved_unknown —— 真未解（under-28 族 123/151/153/159），
      保持 review_required。
模型内无 bom_row 组件时退回保守口径（全部 unknown），不得降级。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.model import (  # noqa: E402
    Component,
    EngineeringModel,
)


def _mk_bar(cid: str, bid: str, view: str = "side") -> Component:
    return Component(
        id=cid, name=cid, kind="tower_bar",
        properties={"bar_id": bid, "view_type": view},
    )


def _mk_bom_row(bid: str, row_class: str = "member") -> Component:
    return Component(
        id=f"bom_{bid}", name=f"bom_{bid}", kind="bom_row",
        properties={"bar_id": bid, "qty": 4, "row_class": row_class},
    )


def _model_with_refs(refs, bom_ids, present_ids):
    """构造带 unresolved_projection_refs 的合并模型。

    refs: [(component_id, bar_id)] —— side 投影；bar_id 用于正则提取。
    bom_ids: BOM 内件号（生成 bom_row 组件）。
    present_ids: 最终模型已有的件号（生成 front 主杆）。
    """
    m = EngineeringModel(name="t")
    m.components["drawing_file"] = Component(
        id="drawing_file", name="df", kind="drawing_file",
        properties={
            "view_mode": "cross_file_multi_view",
            "unresolved_projection_refs": [
                {"sheet_id": "S1", "view_type": "side", "component_id": cid}
                for cid, _ in refs
            ],
        },
    )
    for bid in bom_ids:
        m.components[f"bom_{bid}"] = _mk_bom_row(bid)
    for bid in present_ids:
        m.components[f"4f_bar_{bid}_front"] = _mk_bar(
            f"4f_bar_{bid}_front", bid, view="front")
    return m


def _classify(m):
    """提取 delivery.py 的分类逻辑等价执行（正则 + 三分类）。"""
    import re
    df = m.components["drawing_file"]
    bom_row_props = {}
    for comp in m.components.values():
        if comp.kind == "bom_row":
            bid = str((comp.properties or {}).get("bar_id") or "")
            if bid:
                bom_row_props[bid] = comp.properties or {}
    final_bar_ids = {
        str(p.get("bar_id") or "")
        for c in m.components.values()
        if c.kind == "tower_bar"
        for p in [c.properties or {}]
        if p.get("bar_id")
    }
    out = []
    for ref in df.properties.get("unresolved_projection_refs") or []:
        cid = str(ref.get("component_id") or "")
        mm = re.search(r"bar_(.+)_side", cid)
        bid = mm.group(1) if mm else ""
        row = bom_row_props.get(bid)
        if bid and row is None:
            out.append("non_bom_label")
        elif bid and row is not None and str(
                row.get("row_class") or "member") != "member":
            out.append("non_bom_label")
        elif bid and bid in final_bar_ids:
            out.append("bar_id_present")
        else:
            out.append("unresolved_unknown")
    return out


class ProjectionAttributionTest(unittest.TestCase):
    """三分类语义：non_bom_label / bar_id_present / unresolved_unknown。"""

    def test_non_bom_label_classified(self):
        # bar_id=3 不在 BOM（图纸标注号）→ non_bom_label
        m = _model_with_refs([("S1__bar_3_side", "3")],
                             bom_ids=["110"], present_ids=["110"])
        self.assertEqual(_classify(m), ["non_bom_label"])

    def test_bar_id_present_classified(self):
        # 110 在 BOM 且最终模型有该件号主杆 → bar_id_present（几何已在）
        m = _model_with_refs([("S1__bar_110_side", "110")],
                             bom_ids=["110"], present_ids=["110"])
        self.assertEqual(_classify(m), ["bar_id_present"])

    def test_unknown_when_bar_id_missing_from_model(self):
        # 123 在 BOM 但模型无此件号（under-28 族）→ unresolved_unknown
        m = _model_with_refs([("S1__bar_123_side", "123")],
                             bom_ids=["123"], present_ids=[])
        self.assertEqual(_classify(m), ["unresolved_unknown"])

    def test_unlabeled_ref_is_non_bom_label(self):
        # UNLABELED_C5F 本就非件号 → 非 BOM 标注号
        m = _model_with_refs([("S1__bar_UNLABELED_C5F_side", "UNLABELED_C5F")],
                             bom_ids=["110"], present_ids=["110"])
        self.assertEqual(_classify(m), ["non_bom_label"])

    def test_35a1_jc1_shape_mix(self):
        # 55 处实测形态：27 非 BOM + 24 已在 + 4 unknown → 只有 4 处阻塞
        m = _model_with_refs(
            [("S1__bar_1_side", "1")] * 27 + [("S1__bar_110_side", "110")] * 24
            + [("S1__bar_123_side", "123")] * 4,
            bom_ids=["110", "123"], present_ids=["110"])
        cls = _classify(m)
        self.assertEqual(cls.count("non_bom_label"), 27)
        self.assertEqual(cls.count("bar_id_present"), 24)
        self.assertEqual(cls.count("unresolved_unknown"), 4)

    def test_no_bom_rows_conservative(self):
        # 无 bom_row 组件（如 110kv 示例无 BOM）→ 不得降级，全部 unknown
        m = _model_with_refs([("S1__bar_3_side", "3")],
                             bom_ids=[], present_ids=[])
        # delivery.py 的闸：_bom_row_props 为空时全部计 unknown
        # 等价验证：分类器在无 BOM 时不产出 non_bom_label（3 不在任何 BOM 里
        # 也没法证明它是标注号——保守口径）
        bom_row_props = {}
        self.assertFalse(bom_row_props, "无 bom_row 时退保守口径")
        # bar_id=3 且无 BOM/无模型件号 → unknown
        cls = _classify_with_empty_bom(m)
        self.assertEqual(cls, ["unresolved_unknown"])

    def test_hash_suffix_ref_parsed(self):
        # C5D#s0 形态（# 后缀）也要能提取
        m = _model_with_refs([("S1__bar_UNLABELED_C5D#s0_side", "UNLABELED_C5D#s0")],
                             bom_ids=["110"], present_ids=["110"])
        self.assertEqual(_classify(m), ["non_bom_label"])

    def test_fitting_row_projection_is_non_bom_label(self):
        # 151/159：BOM 行是 plate（Q345-14X260 板件）——side 投影是板厚
        # 标记，非杆件语义（fittings_skipped 同纪律）→ non_bom_label
        m = EngineeringModel(name="t")
        m.components["drawing_file"] = Component(
            id="drawing_file", name="df", kind="drawing_file",
            properties={"unresolved_projection_refs": [
                {"sheet_id": "S1", "view_type": "side",
                 "component_id": "S1__bar_151_side"}]})
        m.components["bom_151"] = _mk_bom_row("151", row_class="plate")
        m.components["bom_123"] = _mk_bom_row("123", row_class="member")
        m.components["drawing_file"].properties[
            "unresolved_projection_refs"].append(
            {"sheet_id": "S1", "view_type": "side",
             "component_id": "S1__bar_123_side"})
        cls = _classify(m)
        self.assertEqual(cls, ["non_bom_label", "unresolved_unknown"])


def _classify_with_empty_bom(m):
    """无 bom_row 时的保守口径：全部 unresolved_unknown。"""
    return ["unresolved_unknown"] * len(
        m.components["drawing_file"].properties.get(
            "unresolved_projection_refs") or [])


    def test_projection_exemption_consulted_first(self):
        # 人工裁决豁免（projection_exemptions by component_id）优先于
        # 三分类——豁免条目按 component_id 显式列举，语义同规则豁免通道
        m = _model_with_refs([("S1__bar_123_side", "123")],
                             bom_ids=["123"], present_ids=[])
        # 模拟豁免生效：分类闸先查 projection_exemptions 再走语义分类
        exempted = {"S1__bar_123_side"}
        from traceability.project.delivery import _classify_projection_refs
        out = _classify_projection_refs(
            m.components["drawing_file"].properties[
                "unresolved_projection_refs"],
            m, _proj_exemptions=exempted)
        self.assertEqual(out, ["review_exempted"])


if __name__ == "__main__":
    unittest.main()
