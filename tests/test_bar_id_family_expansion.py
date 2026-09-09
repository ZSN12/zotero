# -*- coding: utf-8 -*-
"""P4 件号同族扩绑测试（2026-09-08）。

锁定 expand_family_bar_id_binding 的核心语义：
* 候选 = front 面无号物理杆（独立杆/split 链 span 单元），sheet 锚；
* 长度窗 ±3% 全局贪心，每单元只绑一件号；
* 数量闸：扩绑后不超过 BOM qty（已绑 V3 计数参与）；
* 写 bar_id_source=family_expansion 披露，b/l/r 镜像孪生跟随；
* 不碰已绑杆 / 非 primary 次实例 / split 链的单独段（链 span 口径）。
"""

import unittest
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _make_model(bars, dims, nodes=None):
    """bars: [(cid, bar_id, length, face, extra_props)]；
    dims: {bid: (bom_len, sheet, qty)}——detail 写 sheet=/qty= 元数据。"""
    from traceability.model import (
        Component, Dimension, DimensionOrigin, EngineeringModel,
        SourceRef, SourceType,
    )

    m = EngineeringModel(name="test")
    m.add_component(Component(
        id="drawing_file", name="df", kind="drawing_file",
        source=SourceRef(SourceType.DRAWING, "t.dxf"),
        properties={},
    ))
    for nid, (x, y, z) in (nodes or {}).items():
        m.add_component(Component(
            id=nid, name=nid, kind="tower_node",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties={"x": x, "y": y, "z": z},
        ))
    for cid, bid, length, face, extra in bars:
        props = {
            "bar_id": bid,
            "length_mm_3d": length,
            "face": face,
            "geometry_class": "recognized",
        }
        props.update(extra or {})
        m.add_component(Component(
            id=cid, name=cid, kind="tower_bar",
            source=SourceRef(SourceType.DRAWING, "t.dxf"),
            properties=props,
        ))
    for bid, (blen, sheet, qty) in dims.items():
        detail = ""
        if sheet:
            detail += f"sheet={sheet};"
        if qty:
            detail += f"qty={qty}"
        m.dimensions[f"dim_bom_length_{bid}"] = Dimension(
            id=f"dim_bom_length_{bid}", name=f"BOM 长度 {bid}",
            value=blen, unit="mm", origin=DimensionOrigin.MEASURED,
            source=SourceRef(SourceType.DRAWING, "bom.csv", detail=detail),
        )
    return m


class FamilyExpansionTest(unittest.TestCase):

    def test_expansion_binds_unlabeled_to_under_identified(self):
        """107 qty=4 已绑 1 根 + 3 根同长无号 front 杆 → 全部扩绑。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "107", 1284.0, "f", {}),
            ("4f_c_F", None, 1285.0, "f", {}),
            ("4f_d_F", "UNLABELED_X", 1283.0, "f", {}),
            ("4f_e_F", None, 1286.0, "f", {}),
        ]
        m = _make_model(bars, {"107": (1284.0, "S02", 4)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 3)
        for cid in ("4f_c_F", "4f_d_F", "4f_e_F"):
            p = m.components[cid].properties
            self.assertEqual(p["bar_id"], "107")
            self.assertEqual(p["bar_id_source"], "family_expansion")
        # 报告结构
        d = rep["detail"]["107"]
        self.assertEqual(d["bom_qty"], 4)
        self.assertEqual(d["bound_before"], 1)
        self.assertEqual(d["expanded_units"], 3)
        # drawing_file 披露
        self.assertIn("bar_id_expansion_report",
                      m.components["drawing_file"].properties)

    def test_qty_cap_respects_existing_binding(self):
        """110 qty=8 已绑 6 根（V3 计数），候选再多也只扩 2。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [("4f_bound%d_F" % i, "110", 1023.0, "f", {}) for i in range(6)]
        bars += [("4f_u%d_F" % i, None, 1024.0, "f", {}) for i in range(4)]
        m = _make_model(bars, {"110": (1023.0, "S02", 8)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["detail"]["110"]["expanded_units"], 2)
        bound = sum(
            1 for cid, c in m.components.items()
            if c.kind == "tower_bar"
            and c.properties.get("bar_id") == "110"
            and c.properties.get("bar_id_source") != "family_expansion")
        self.assertEqual(bound, 6)

    def test_phys_budget_counts_mirror_quadruplet_as_four(self):
        """数量闸按物理根数：1 个 front 单元 + b/l/r 镜像 = 4 根预算。

        qty=4 已绑 1 根 → 缺口 3；一个四面分离的单元（4 根）超缺口 → 跳过。
        101 实证（JC1 首跑）：pbase_spoke 四面 4 分量，qty=1 时旧闸按
        单元数放行 → 写入 4 根 > 1 超计。
        """
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        nodes = {
            "nf": (0.0, 0.0, 0.0), "nt": (0.0, 0.0, 1284.0),
            "bf": (2000.0, 0.0, 0.0), "bt": (2000.0, 0.0, 1284.0),
            "lf": (0.0, 2000.0, 0.0), "lt": (0.0, 2000.0, 1284.0),
            "rf": (0.0, -2000.0, 0.0), "rt": (0.0, -2000.0, 1284.0),
        }
        bars = [
            ("4f_a_F", "107", 1284.0, "f",
             {"from_node": "nf", "to_node": "nt"}),
            # 四面分离镜像族：几何互不连通 → 4 分量 = 4 物理根
            ("4f_x_F", None, 1284.0, "f",
             {"from_node": "nf", "to_node": "nt"}),
            ("4f_x_B", None, 1284.0, "b",
             {"from_node": "bf", "to_node": "bt"}),
            ("4f_x_L", None, 1284.0, "l",
             {"from_node": "lf", "to_node": "lt"}),
            ("4f_x_R", None, 1284.0, "r",
             {"from_node": "rf", "to_node": "rt"}),
        ]
        m = _make_model(bars, {"107": (1284.0, "S02", 4)}, nodes)
        rep = expand_family_bar_id_binding(m)
        # 缺口 = 4 - 1 = 3 根；单元预算 4 根 > 3 → 不扩
        self.assertEqual(rep["expanded_units"], 0)
        self.assertIsNone(m.components["4f_x_F"].properties.get("bar_id"))

    def test_single_qty_ids_are_not_expanded(self):
        """qty=1 件号不扩绑：图面标注即全部（「只标一处」前提不成立）。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "101", 1284.0, "f", {}),
            ("4f_x_F", None, 1284.0, "f", {}),
        ]
        m = _make_model(bars, {"101": (1284.0, "S02", 1)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 0)
        self.assertIsNone(m.components["4f_x_F"].properties.get("bar_id"))

    def test_sheet_anchor_excludes_cross_sheet_candidates(self):
        """BOM 行 sheet=S02 时 S05 的同长候选被排除（跨册闸）。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "107", 1284.0, "f", {"source_file": "S02"}),
            ("4f_x_F", None, 1284.0, "f", {"source_file": "S05"}),
        ]
        m = _make_model(bars, {"107": (1284.0, "S02", 4)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 0)
        self.assertIsNone(m.components["4f_x_F"].properties.get("bar_id"))

    def test_length_window_excludes_out_of_tolerance(self):
        """偏差 >3% 的候选不进窗。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "107", 1284.0, "f", {}),
            ("4f_x_F", None, 1400.0, "f", {}),   # +9%
        ]
        m = _make_model(bars, {"107": (1284.0, "S02", 4)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 0)

    def test_greedy_assigns_each_unit_to_one_id(self):
        """同长族两件号争抢一根候选 → 只有偏差更小的件号得到。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "112", 913.0, "f", {}),
            ("4f_b_F", "115", 927.0, "f", {}),
            ("4f_x_F", None, 914.0, "f", {}),    # |914-913|=1 vs |914-927|=13
        ]
        m = _make_model(bars, {
            "112": (913.0, "S02", 4), "115": (927.0, "S02", 4)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(m.components["4f_x_F"].properties["bar_id"], "112")
        self.assertEqual(rep["detail"].get("115", {}).get("expanded_units", 0), 0)

    def test_split_chain_binds_as_span_unit(self):
        """split 链（同 root）按整链 span 配对——链内段不单独绑短件号。

        CLE0010 实证（JC1）：链 span 915.6 绑 112（913，0.28%）；若把
        首段 321 单独绑 121（320）会让链 span 对 BOM 超差 185%。
        """
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        nodes = {f"n{i}": (0.0, 0.0, float(i * 300)) for i in range(4)}
        bars = [
            ("4f_bound_F", "112", 913.0, "f", {}),   # 112 已绑锚（同族背书）
            ("4f_b1_F", None, 300.0, "f",
             {"root_bar_id": "r1", "from_node": "n0", "to_node": "n1"}),
            ("4f_b1__split1_F", None, 300.0, "f",
             {"root_bar_id": "r1", "from_node": "n1", "to_node": "n2"}),
            ("4f_b1__split2_F", None, 300.0, "f",
             {"root_bar_id": "r1", "from_node": "n2", "to_node": "n3"}),
        ]
        # 121 BOM=320（≈链首段）+ 112 BOM=913（≈链 span 900）都开窗
        m = _make_model(bars, {
            "121": (320.0, "S02", 4), "112": (913.0, "S02", 4)})
        rep = expand_family_bar_id_binding(m)
        # 链作为整单元配 112（span 900，dev 1.4%）；首段 300 与 121 的
        # 配对（dev 6%）不在 3% 窗内，且单元是整链——121 不可能拿到
        self.assertEqual(m.components["4f_b1_F"].properties.get("bar_id"), "112")
        self.assertEqual(m.components["4f_b1__split1_F"].properties.get("bar_id"), "112")
        self.assertNotIn("121", rep["detail"])

    def test_mirror_siblings_follow_front(self):
        """front 扩绑后 b/l/r 同 stem 镜像跟随（同一物理杆的展开实例）。

        四面共线（同几何键）→ 1 物理根预算；qty=4 已绑 1 根，缺口 3 ≥ 1。
        """
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        nodes = {"n0": (0.0, 0.0, 0.0), "n1": (0.0, 0.0, 1284.0)}
        bars = [
            ("4f_a_F", "107", 1284.0, "f",
             {"from_node": "n0", "to_node": "n1"}),
            ("4f_x_F", None, 1284.0, "f",
             {"from_node": "n0", "to_node": "n1"}),
            ("4f_x_B", None, 1284.0, "b",
             {"from_node": "n0", "to_node": "n1"}),
            ("4f_x_L", None, 1284.0, "l",
             {"from_node": "n0", "to_node": "n1"}),
        ]
        m = _make_model(bars, {"107": (1284.0, "S02", 4)}, nodes)
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 1)   # front 单元 1 个
        self.assertEqual(m.components["4f_x_B"].properties["bar_id"], "107")
        self.assertEqual(m.components["4f_x_L"].properties["bar_id"], "107")
        self.assertTrue(m.components["4f_x_B"].properties.get("bar_id_expansion_mirror"))

    def test_does_not_touch_dup_secondary_instances(self):
        """intake 非 primary 次实例（bar_id_dup 且 primary=False）不作锚。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "107", 1284.0, "f", {}),
            ("4f_x_F", "UNLABELED_Y", 1284.0, "f",
             {"bar_id_dup": True, "bar_id_primary": False}),
        ]
        m = _make_model(bars, {"107": (1284.0, "S02", 4)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 0)
        # 原值不动
        self.assertEqual(m.components["4f_x_F"].properties["bar_id"], "UNLABELED_Y")

    def test_section_gate_excludes_mismatched_bom_section(self):
        """截面闸：候选 section=L40X3 vs BOM L50X4 → 排除（304 实证）。

        同一物理杆不可能既是 L40X3 又是 L50X4——长度窗撞号是巧合。
        """
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "304", 2959.0, "f", {"section": "L50X4"}),
            ("4f_x_F", None, 2939.0, "f", {"section": "L40X3"}),
            ("4f_y_F", None, 2950.0, "f", {"section": None}),
        ]
        m = _make_model(bars, {"304": (2959.0, "S04", 4)})
        from traceability.model import Dimension, DimensionOrigin, SourceRef, SourceType
        m.dimensions["dim_bom_section_304"] = Dimension(
            id="dim_bom_section_304", name="BOM 截面 304",
            value="L50X4", unit="", origin=DimensionOrigin.MEASURED,
            source=SourceRef(SourceType.DRAWING, "bom.csv"))
        rep = expand_family_bar_id_binding(m)
        # L40X3 候选被截面闸排除；无截面候选（4f_y_F）可进
        self.assertIsNone(m.components["4f_x_F"].properties.get("bar_id"))
        self.assertEqual(m.components["4f_y_F"].properties.get("bar_id"), "304")

    def test_zero_bound_ids_are_not_expanded(self):
        """bound=0 的件号不扩：无已绑实例即无同族锚（截面/位置背书），
        纯长度巧合撞号风险高（304/510 等 BOM_PENDING 族实证）。"""
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [
            ("4f_a_F", "304", 2959.0, "f", {}),
            ("4f_x_F", None, 2950.0, "f", {}),
        ]
        # 416 qty=4 bound=0：不在 targets
        m = _make_model(bars, {
            "304": (2959.0, "S04", 4), "416": (2959.0, "S04", 4)})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 1)   # 只有 304 是目标
        self.assertNotIn("416", rep["detail"])

    def test_no_bom_dimensions_is_noop(self):
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [("4f_x_F", None, 1000.0, "f", {})]
        m = _make_model(bars, {})
        rep = expand_family_bar_id_binding(m)
        self.assertEqual(rep["expanded_units"], 0)
        self.assertEqual(m.components["4f_x_F"].properties.get("bar_id"), None)

    def test_report_written_to_drawing_file(self):
        from traceability.project.bar_id_expansion import (
            expand_family_bar_id_binding,
        )
        bars = [("4f_a_F", "107", 1284.0, "f", {}),
                ("4f_x_F", None, 1284.0, "f", {})]
        m = _make_model(bars, {"107": (1284.0, "S02", 4)})
        expand_family_bar_id_binding(m)
        rep = m.components["drawing_file"].properties["bar_id_expansion_report"]
        self.assertTrue(rep["enabled"])
        self.assertIn("107", rep["detail"])
        self.assertIn("length_tol", rep)


if __name__ == "__main__":
    unittest.main()
