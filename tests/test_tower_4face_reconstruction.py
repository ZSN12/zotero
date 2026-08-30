"""Phase 1–3 铁塔三维可信重构验收测试。

覆盖：
    Phase 1  fit_leg_worklines / snap_diagonals_to_legs（公垂线中点吸附）
    Phase 1b close_face_intersections（T 形交点打断闭合）
    Phase 2  expand_4_face_symmetry（四面镜像 + 四角主腿熔合 + 横隔面）
             inspect_model_topology（Degree=1 悬空节点统计）
    Phase 3  MODULE_DEFINITIONS（M1–M6）/ split_merged_by_modules /
             assemble_modules(rigid=True) / r_project_assembly_closed
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from traceability.solve import tower_geometry as g
from traceability.project.module_build import (
    MODULE_DEFINITIONS,
    split_merged_by_modules,
    try_assembly_m1_m6_from_merged,
)
from traceability.project.assembly import align_boundary_pair
from traceability.model import Component, EngineeringModel


class Phase1WorklineTest(unittest.TestCase):
    def test_fit_leg_worklines(self):
        nodes = {
            "L1": (100.0, 0.0, 0.0), "L2": (100.0, 0.0, 100.0),
            "R1": (-100.0, 0.0, 0.0), "R2": (-100.0, 0.0, 100.0),
        }
        bars = [
            {"id": "leg_l", "from": "L1", "to": "L2"},
            {"id": "leg_r", "from": "R1", "to": "R2"},
        ]
        wl = g.fit_leg_worklines(nodes, bars, leg_ids=["leg_l", "leg_r"])
        self.assertEqual(len(wl), 2)
        for _, p0, v, rms in wl:
            self.assertLess(rms, 1e-6)
            self.assertAlmostEqual(abs(v[2]), 1.0, places=6)
            self.assertAlmostEqual(abs(p0[0]), 100.0, places=6)

    def test_snap_diagonal_uses_common_perpendicular_midpoint(self):
        nodes = {
            "L1": (100.0, 0.0, 0.0), "L2": (100.0, 0.0, 100.0),
            "D1": (0.0, 0.0, 50.0), "D2": (95.0, 0.0, 60.0),
        }
        bars = [
            {"id": "leg", "from": "L1", "to": "L2"},
            {"id": "diag", "from": "D1", "to": "D2"},
        ]
        nn, nb = g.snap_diagonals_to_legs(nodes, bars, leg_ids=["leg"], snap_tol=80.0)
        diag = next(b for b in nb if b["id"] == "diag")
        to = nn[diag["to"]]
        # 公垂线中点落在主腿工作线 x=100 上
        self.assertAlmostEqual(to[0], 100.0, places=1)

    def test_close_face_intersections_merges_t_junction(self):
        # 斜材端点 (60,0,50) 落在水平杆 (0,0,50)-(100,0,50) 线段内
        nodes = {
            "H1": (0.0, 0.0, 50.0), "H2": (100.0, 0.0, 50.0),
            "D1": (60.0, 0.0, 0.0), "D2": (60.0, 0.0, 50.0),
        }
        bars = [
            {"id": "horiz", "from": "H1", "to": "H2"},
            {"id": "diag", "from": "D1", "to": "D2"},
        ]
        nn, nb = g.close_face_intersections(nodes, bars, snap_tol=5.0)
        topo = g.inspect_model_topology(nn, nb)
        # 水平杆在交点处被拆成两段，斜材端点共享该交点（不再悬空在 D2）
        self.assertGreaterEqual(topo["total_bars"], 3)
        d2_id = next(nid for nid, p in nn.items() if abs(p[0] - 60.0) < 1e-6 and abs(p[2] - 50.0) < 1e-6)
        deg_d2 = sum(1 for b in nb if b["from"] == d2_id or b["to"] == d2_id)
        self.assertGreaterEqual(deg_d2, 2)

    def test_close_face_intersections_sets_root_bar_id_and_split_index(self):
        # 阶段1.2：拆分杆必须带 root_bar_id / split_index 溯源，且不残留
        # 「未拆分的原杆」——原杆只被截断为首段，不允许整根原杆同时存在。
        nodes = {
            "H1": (0.0, 0.0, 50.0), "H2": (100.0, 0.0, 50.0),
            "D1": (60.0, 0.0, 0.0), "D2": (60.0, 0.0, 50.0),
        }
        bars = [
            {"id": "horiz", "from": "H1", "to": "H2", "derived_from": "horiz_src"},
            {"id": "diag", "from": "D1", "to": "D2"},
        ]
        nn, nb = g.close_face_intersections(nodes, bars, snap_tol=5.0)
        by_id = {b["id"]: b for b in nb}
        horiz = by_id["horiz"]
        splits = [b for b in nb if b["id"].startswith("horiz__split")]
        # 原杆被截断为首段（split_index=0），并带 root_bar_id 指向自己
        self.assertEqual(horiz["root_bar_id"], "horiz")
        self.assertEqual(horiz["split_index"], 0)
        self.assertEqual(horiz["derived_from"], "horiz_src")   # 溯源保留
        # 新拆分段 split_index=1，root 指向原杆
        self.assertEqual(len(splits), 1)
        self.assertEqual(splits[0]["root_bar_id"], "horiz")
        self.assertEqual(splits[0]["split_index"], 1)
        self.assertEqual(splits[0]["derived_from"], "horiz_src")
        # 不允许整根原杆（H1->H2 全长）同时存在：所有段拼起来才是原杆
        for b in nb:
            if b["id"] in ("horiz",) or b["id"].startswith("horiz__split"):
                self.assertFalse(b["from"] == "H1" and b["to"] == "H2")

    def test_close_face_intersections_recursive_split_preserves_root(self):
        # 递归拆分（同一根杆被二次打断）时 root_bar_id 仍指向最原始杆
        nodes = {
            "H1": (0.0, 0.0, 50.0), "H2": (100.0, 0.0, 50.0),
            "D1": (60.0, 0.0, 0.0), "D2": (60.0, 0.0, 50.0),
            "E1": (80.0, 0.0, 0.0), "E2": (80.0, 0.0, 50.0),
        }
        bars = [
            {"id": "horiz", "from": "H1", "to": "H2"},
            {"id": "diag", "from": "D1", "to": "D2"},
            {"id": "diag2", "from": "E1", "to": "E2"},
        ]
        nn, nb = g.close_face_intersections(nodes, bars, snap_tol=5.0)
        for b in nb:
            if b["id"].startswith("horiz"):
                self.assertEqual(b["root_bar_id"], "horiz")


class Phase2FourFaceTest(unittest.TestCase):
    def test_expand_4_face_and_diaphragms(self):
        nodes = {
            "A": (-100.0, 0.0, 0.0), "B": (100.0, 0.0, 0.0),
            "C": (-100.0, 0.0, 100.0), "D": (100.0, 0.0, 100.0),
        }
        bars = [
            {"id": "leg_l", "from": "A", "to": "C"},
            {"id": "leg_r", "from": "B", "to": "D"},
            {"id": "horiz_bot", "from": "A", "to": "B"},
            {"id": "horiz_top", "from": "C", "to": "D"},
            {"id": "diag", "from": "A", "to": "D"},
        ]
        nn, nb = g.expand_4_face_symmetry(nodes, bars, wall=100.0)
        # 4 个面 + 四角主腿 + 2 个标高平台（上下各 1）横隔
        faces = {b.get("face") for b in nb}
        self.assertIn("f", faces)
        self.assertIn("b", faces)
        self.assertIn("l", faces)
        self.assertIn("r", faces)
        self.assertGreaterEqual(sum(1 for b in nb if b.get("diaphragm")), 2)
        # 四角主腿：每个拐角（±100,±100）至少两条共点杆件（前后/左右面腿熔合）
        corner_nodes = [nid for nid, p in nn.items()
                        if abs(abs(p[0]) - 100.0) < 1e-6 and abs(abs(p[1]) - 100.0) < 1e-6]
        self.assertGreaterEqual(len(corner_nodes), 4)
        for nid in corner_nodes:
            deg = sum(1 for b in nb if b["from"] == nid or b["to"] == nid)
            self.assertGreaterEqual(deg, 2, f"拐角节点 {nid} 度数应 >= 2")

    def test_inspect_topology_reports_degree1(self):
        nodes = {"A": (0.0, 0.0, 0.0), "B": (100.0, 0.0, 0.0)}
        bars = [{"id": "solo", "from": "A", "to": "B"}]
        topo = g.inspect_model_topology(nodes, bars)
        self.assertEqual(topo["dangling_degree1"], 2)

    def test_expand_preserves_front_xz_invariant(self):
        # 阶段1.4：四面展开前后 front（F 面）几何不变——F 面杆端点 x/z 必须与
        # 原 front 杆完全一致（y 由镜像半宽合成可不同），不得被镜像/吸附扭曲。
        # 注：用无竖腿几何（水平 + 斜材）避开角腿去重（|leg_x|==half_width 时
        # 镜像角点重合是阶段6已归档问题），干净断言 front x/z 不变。
        from traceability.intake.tower_symmetry import expand_4_face_symmetry_model
        from traceability.model import SourceRef, SourceType
        m = EngineeringModel(name="tower")
        m.add_component(Component(
            id="drawing_file", name="df", kind="drawing_file",
            source=SourceRef(SourceType.DRAWING, "x.dxf"),
            properties={"view_kinds": ["front"]},
        ))
        pts = {
            "A": (-100.0, 0.0, 0.0), "B": (100.0, 0.0, 0.0),
            "C": (-100.0, 0.0, 100.0), "D": (100.0, 0.0, 100.0),
        }
        for nid, (x, y, z) in pts.items():
            m.add_component(Component(
                id=nid, name=nid, kind="tower_node",
                source=SourceRef(SourceType.DRAWING, "x.dxf"),
                properties={"view_type": "front", "x": x, "y": y, "z": z,
                            "solve_status": "solved"},
            ))
        bar_defs = [
            ("horiz_bot", "A", "B"), ("horiz_top", "C", "D"), ("diag", "A", "D"),
        ]
        for bid, f, t in bar_defs:
            m.add_component(Component(
                id=bid, name=bid, kind="tower_bar",
                source=SourceRef(SourceType.DRAWING, "x.dxf"),
                properties={"bar_id": bid, "view_type": "front",
                            "from_node": f, "to_node": t,
                            "geometry_origin": "dxf_geom",
                            "solve_status": "solved"},
            ))
        expand_4_face_symmetry_model(
            m,
            overlay={"stitch_boundaries": False, "snap_diagonals": False,
                     "close_face_intersections": False},
            weld_corner_legs=False, add_diaphragms=False,
        )

        bars = {cid: c for cid, c in m.components.items() if c.kind == "tower_bar"}
        nodes = {cid: c for cid, c in m.components.items() if c.kind == "tower_node"}
        f_bars = {cid: c for cid, c in bars.items()
                  if c.properties.get("face") == "f"
                  and c.properties.get("geometry_class") == "recognized"}
        # 每根 front 杆都应有 F 面 recognized 副本，端点 x/z 与原图一致
        self.assertEqual(len(f_bars), len(bar_defs))
        for bid, f, t in bar_defs:
            fc = f_bars.get(f"4f_{bid}_F")
            self.assertIsNotNone(fc, f"缺少 F 面杆 4f_{bid}_F")
            fn = nodes[fc.properties["from_node"]].properties
            tn = nodes[fc.properties["to_node"]].properties
            self.assertAlmostEqual(fn["x"], pts[f][0], places=3)
            self.assertAlmostEqual(fn["z"], pts[f][2], places=3)
            self.assertAlmostEqual(tn["x"], pts[t][0], places=3)
            self.assertAlmostEqual(tn["z"], pts[t][2], places=3)
        # 展开后原始（非 4f_ 前缀）杆件不复存在——统一被 4f_* 替换
        self.assertFalse(any(not cid.startswith("4f_") for cid in bars))
        # 四面齐全（F/B/L/R 生成面）
        faces = {c.properties.get("generated_face") for c in bars.values()}
        self.assertLessEqual({"F", "B", "L", "R"}, faces)


class Phase3ModuleChainTest(unittest.TestCase):
    def test_module_definitions_are_m1_m6(self):
        self.assertEqual([m["id"] for m in MODULE_DEFINITIONS],
                         ["M1_LEG", "M2_LOWER_BODY", "M3_MID_BODY",
                          "M4_UPPER_BODY", "M5_CROSSARM", "M6_HEAD"])

    def _make_tower_model(self):
        m = EngineeringModel(name="tower")
        # 四角主腿：z 从 0 到 36600，四个拐角
        w = 2000.0
        corners = [(w, w), (-w, w), (-w, -w), (w, -w)]
        for ci, (cx, cy) in enumerate(corners):
            for z in (0.0, 9000.0, 18000.0, 24000.0, 30000.0, 33500.0, 36600.0):
                cid = f"n{ci}_{int(z)}"
                m.add_component(Component(id=cid, name=cid, kind="tower_node",
                                          properties={"x": cx, "y": cy, "z": z,
                                                      "solve_status": "solved"}))
            for z0, z1 in zip((0.0, 9000.0, 18000.0, 24000.0, 30000.0, 33500.0),
                              (9000.0, 18000.0, 24000.0, 30000.0, 33500.0, 36600.0)):
                bid = f"leg{ci}_{int(z0)}"
                m.add_component(Component(
                    id=bid, name=bid, kind="tower_bar",
                    properties={"bar_id": bid,
                                "from_node": f"n{ci}_{int(z0)}",
                                "to_node": f"n{ci}_{int(z1)}",
                                "solve_status": "solved"}))
        return m

    def test_split_m1_m6_and_rigid_assembly_closed(self):
        m = self._make_tower_model()
        mods = split_merged_by_modules(m, interface_tol_mm=500.0)
        self.assertEqual(len(mods), 6)
        # 每个模块都有节点与杆件
        for mod in mods:
            self.assertGreater(sum(1 for c in mod.components.values() if c.kind == "tower_node"), 0)
            self.assertGreater(sum(1 for c in mod.components.values() if c.kind == "tower_bar"), 0)

        from traceability.project.assembly import assemble_modules
        asm, reports = assemble_modules(mods, tol_mm=5.0, rigid=True)
        self.assertEqual(len(reports), 5)
        for r in reports:
            self.assertTrue(r["closed"], r)
            self.assertLessEqual(r["max_gap_mm"], 5.0)

    def test_align_boundary_pair_rigid(self):
        lower = EngineeringModel(name="lower")
        upper = EngineeringModel(name="upper")
        # 同一几何，upper 整体平移+旋转
        # upper 为 lower 的小幅刚体变换（XY 位移在 tol 内 + Z 整体抬高），
        # 验证 Kabsch [R|T] 能把接口重新闭合。
        src = [(1000.0, 1000.0, 0.0), (-1000.0, 1000.0, 0.0),
               (-1000.0, -1000.0, 0.0), (1000.0, -1000.0, 0.0)]
        offset = (3.0, -4.0, 4500.0)
        for i, (x, y, z) in enumerate(src):
            lower.add_component(Component(id=f"l{i}", name=f"l{i}", kind="tower_node",
                                          properties={"x": x, "y": y, "z": 9000.0,
                                                      "solve_status": "solved"}))
            upper.add_component(Component(id=f"u{i}", name=f"u{i}", kind="tower_node",
                                          properties={"x": x + offset[0], "y": y + offset[1],
                                                      "z": z + offset[2], "solve_status": "solved"}))
        report = align_boundary_pair(lower, upper, tol_mm=5.0, rigid=True)
        self.assertTrue(report["closed"], report)
        self.assertLessEqual(report["max_gap_mm"], 5.0)


if __name__ == "__main__":
    unittest.main()
