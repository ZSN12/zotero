"""Phase 6.5 回归测试：节点板样例 GLB（TASK_VIEWER_POLISH 合入门槛）。

覆盖：
    * 合成详图页 fixture → build_sample 产出板体 + 每组孔片 + 示意杆，
      bar_map 与 GLB mesh 数一致、component_id 一一对应
    * CLI 全链：导出 detail_sample.glb 存在且 ≥1 mesh
    * 真实 03 页（存在时）：56 孔全部落在板体轮廓内（凸包修复的有效性）
"""
from __future__ import annotations

import importlib.util
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bs_mod = _load_script("build_detail_sample")

REAL_SHEET = REPO / "web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json"


def _fixture_sheet() -> dict:
    """1 节点板（碎片多边形）+ 2 螺栓组（孔散布在碎片外，考验凸包修复）。"""
    def group(gid, holes):
        return {gid: {"id": gid, "kind": "bolt_group", "name": gid, "properties": {
            "group_id": gid, "count": len(holes), "diameter_mm": 16.0,
            "hole_diameter_mm": 17.5, "length_mm": 40.0, "holes": holes,
            "solve_status": "pending_review"}}}

    comps = {
        "gusset_D9": {"id": "gusset_D9", "kind": "gusset_plate", "name": "gusset_D9",
                      "properties": {
                          "detail_id": "D9", "polygon_local": [[0, 0], [5, 0], [5, 5], [0, 5]],
                          "thickness_mm": None, "bolt_holes": [], "material": "",
                          "solve_status": "pending_review",
                          "transform": {"scale_to_real": 1.0, "origin_local": [0, 0]}}},
        "drawing_view": {"id": "dv", "kind": "drawing_file", "name": "dv", "properties": {}},
    }
    comps.update(group("bolt_group_D9_B1", [[-60, -40], [60, 40], [-80, 50]]))
    comps.update(group("bolt_group_D9_B2", [[80, -60], [-40, 70]]))
    return {"name": "fixture", "components": comps}


def _glb_node_names(glb_path: Path):
    data = glb_path.read_bytes()
    jlen = struct.unpack("<I", data[12:16])[0]
    j = json.loads(data[20:20 + jlen])
    return [n.get("name", "") for n in j.get("nodes", [])]


class TestBuildDetailSample(unittest.TestCase):
    def test_fixture_scene_counts_and_names(self):
        sheet = _fixture_sheet()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sheet.json"
            p.write_text(json.dumps(sheet), encoding="utf-8")
            detail = bs_mod.parse_detail_sheet(p)
            self.assertEqual(detail["detail_id"], "D9")
            self.assertEqual(len(detail["groups"]), 2)
            scene, bar_map = bs_mod.build_sample(detail, 8.0, 25.0, "L100X10")
            # 板 1 + 孔组 2 + 示意杆（2 组 × 两侧 = 4）
            kinds = [e["kind"] for e in bar_map]
            self.assertEqual(kinds.count("gusset_plate"), 1)
            self.assertEqual(kinds.count("bolt_holes"), 2)
            self.assertEqual(kinds.count("bar_stub_schematic"), 4)
            self.assertEqual(bar_map[0]["n_holes"], 5)
            self.assertTrue(bar_map[0]["thickness_assumed"])
            # bar_map ↔ GLB mesh 一一对应
            out = Path(td) / "d.glb"
            scene.export(str(out))
            names = _glb_node_names(out)
            self.assertEqual(len(names), len(bar_map))
            self.assertEqual(set(names), {e["component_id"] for e in bar_map})

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sheet = tmp / "sheet.json"
            sheet.write_text(json.dumps(_fixture_sheet()), encoding="utf-8")
            rc = bs_mod.main(["--sheet", str(sheet), "--out-dir", str(tmp / "out")])
            self.assertEqual(rc, 0)
            glb = tmp / "out" / "detail_sample.glb"
            self.assertTrue(glb.exists())
            self.assertGreaterEqual(len(_glb_node_names(glb)), 1)
            bm = json.loads((tmp / "out" / "detail_sample.bar_map.json")
                            .read_text(encoding="utf-8"))
            self.assertEqual(len(bm), len(_glb_node_names(glb)))

    @unittest.skipUnless(REAL_SHEET.exists(), "真实 03 详图页未同步")
    def test_real_sheet_holes_inside_repaired_outline(self):
        """真实数据：凸包修复后所有 56 孔必须落在板轮廓内（样例有效性核心）。"""
        detail = bs_mod.parse_detail_sheet(REAL_SHEET)
        self.assertEqual(detail["detail_id"], "D1")
        n_holes = sum(len(g["holes"]) for g in detail["groups"])
        self.assertGreaterEqual(n_holes, 32)
        scene, bar_map = bs_mod.build_sample(detail, 8.0, 25.0, "Q345L100X7")
        plate = next(g for name, g in scene.geometry.items()
                     if name.startswith("detail_gusset"))
        import numpy as np

        def cross2(u, v):   # 二维叉积标量（避开 numpy 2.0 对 2D cross 的弃用告警）
            return u[0] * v[1] - u[1] * v[0]
        ring = plate.vertices[plate.vertices[:, 2] < 1e-6][:, :2]  # 底面环
        # 凸多边形包含判定：对所有边同侧
        holes = [(x, y) for g in detail["groups"] for (x, y) in g["holes"]]
        cx = sum(h[0] for h in holes) / len(holes)
        cy = sum(h[1] for h in holes) / len(holes)
        order = np.arctan2(ring[:, 1] - ring[:, 1].mean(), ring[:, 0] - ring[:, 0].mean())
        ring = ring[np.argsort(order)]
        inside = True
        for (hx, hy) in holes:
            pt = np.array([hx - cx, hy - cy])
            s = set()
            for i in range(len(ring)):
                a, b = ring[i], ring[(i + 1) % len(ring)]
                s.add(cross2(b - a, pt - a) >= -1e-6)
            if len(s) != 1:
                inside = False
                break
        self.assertTrue(inside, "有孔落在修复轮廓外")


if __name__ == "__main__":
    unittest.main()
