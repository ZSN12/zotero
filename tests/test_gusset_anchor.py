"""M4：大样节点板全局锚定测试。"""

from __future__ import annotations

import unittest

from traceability.connection.detail_view import parse_detail_view_meta
from traceability.connection.gusset_anchor import (
    _detail_center_vx_vy,
    anchor_gusset_to_node,
    auto_anchor_gussets,
)
from traceability.model import Component, EngineeringModel


class GussetAnchorTest(unittest.TestCase):
    def test_detail_center_maps_to_front_local(self):
        detail = {
            "region": [34383.0, 34717.0, -8332.0, -8006.0],
            "origin": [34383.0, -8006.0],
        }
        front = {"origin": [34350.0, -7244.0]}
        vx, vy = _detail_center_vx_vy(detail, front)
        self.assertAlmostEqual(vx, 200.0)
        self.assertAlmostEqual(vy, -925.0)

    def test_anchor_writes_polygon_global(self):
        from traceability.connection.gusset import parse_gusset_from_detail

        model = EngineeringModel(name="t")
        t = parse_detail_view_meta("节点 K1 大样 1:10")
        plate = parse_gusset_from_detail(
            "K1", [(0, 0), (100, 0), (100, 80), (0, 80)], transform=t,
        )
        model.add_component(plate.to_component())
        model.add_component(Component(
            id="node_A", name="n", kind="tower_node",
            properties={
                "x": 1000.0, "y": 2000.0, "z": 8100.0,
                "view_type": "front", "view_x": 50.0, "view_y": 100.0,
                "solve_status": "solved",
            },
        ))
        ok = anchor_gusset_to_node(model, "gusset_K1", "node_A", anchor_origin="test")
        self.assertTrue(ok)
        g = model.components["gusset_K1"]
        self.assertEqual(g.properties.get("solve_status"), "verified")
        self.assertGreaterEqual(len(g.properties.get("polygon_global") or []), 3)

    def test_auto_anchor_respects_overlay_explicit(self):
        from traceability.connection.gusset import parse_gusset_from_detail
        from traceability.connection.detail_view import parse_detail_view_meta

        model = EngineeringModel(name="t")
        t = parse_detail_view_meta("detail")
        plate = parse_gusset_from_detail("D1", [(0, 0), (50, 0), (50, 50)], transform=t)
        comp = plate.to_component()
        comp.properties["source_file"] = "sheet03"
        model.add_component(comp)
        model.add_component(Component(
            id="node_target", name="n", kind="tower_node",
            properties={"x": 1.0, "y": 2.0, "z": 3.0, "solve_status": "solved", "view_type": "front"},
        ))
        overlay = {
            "gusset_anchors": {"gusset_D1": "node_target"},
        }
        rep = auto_anchor_gussets(model, overlay=overlay)
        self.assertEqual(len(rep["anchored"]), 1)
        self.assertEqual(rep["anchored"][0]["node"], "node_target")


# --------------------------------------------------------------------------- #
# LOD 跃迁阶段 3（任务 G）：薄壳生成 + 全塔节点锚定
# --------------------------------------------------------------------------- #
import inspect
import json
import struct
from pathlib import Path

from traceability.connection import bolt_mesh, gusset, gusset_anchor
from traceability.model import Component, EngineeringModel, SourceRef, SourceType

REPO = Path(__file__).resolve().parent.parent
SOLID_DIR = REPO / "out/35A1-JC1-solid"


def _glb_json(path: Path) -> dict:
    data = path.read_bytes()
    assert data[:4] == b"glTF", "不是 GLB"
    jlen = struct.unpack("<I", data[12:16])[0]
    return json.loads(data[20:20 + jlen])


def _synth_model():
    """小塔：z=18000 一层四角节点 + 每角 3 根交汇杆（上/下/横）。"""
    m = EngineeringModel(name="synth")
    corners = [(500, 500), (-500, 500), (-500, -500), (500, -500)]
    for i, (x, y) in enumerate(corners):
        for z, tag in ((12000, "lo"), (18000, "mid"), (24000, "hi")):
            m.add_component(Component(id=f"N_{tag}{i}", name=f"N_{tag}{i}", kind="tower_node",
                                      properties={"x": x, "y": y, "z": z}))
        m.add_component(Component(id=f"B_up{i}", name=f"B_up{i}", kind="tower_bar",
                                  properties={"from_node": f"N_mid{i}", "to_node": f"N_hi{i}",
                                              "role": "DIAG", "section": "L100X7"}))
        m.add_component(Component(id=f"B_dn{i}", name=f"B_dn{i}", kind="tower_bar",
                                  properties={"from_node": f"N_lo{i}", "to_node": f"N_mid{i}",
                                              "role": "LEG", "section": "L140X10"}))
        m.add_component(Component(id=f"B_hz{i}", name=f"B_hz{i}", kind="tower_bar",
                                  properties={"from_node": f"N_mid{i}", "to_node": f"N_mid{(i+1)%4}",
                                              "role": "HORIZ", "section": "L56X4"}))
    return m


class TestGussetShell(unittest.TestCase):
    def test_rect_watertight_volume(self):
        mesh = gusset.make_gusset_shell([(0, 0), (100, 0), (100, 80), (0, 80)], 6.0)
        self.assertTrue(mesh.is_watertight)
        self.assertAlmostEqual(mesh.volume, 100 * 80 * 6, delta=1.0)

    def test_convex_polygon_and_closing_point(self):
        hexa = [(50 + 40 * __import__("math").cos(a), 40 + 40 * __import__("math").sin(a))
                for a in [i * __import__("math").pi / 3 for i in range(6)]]
        hexa.append(hexa[0])  # 重复闭合点应被清理
        mesh = gusset.make_gusset_shell(hexa, 8.0)
        self.assertTrue(mesh.is_watertight)

    def test_degenerate_inputs_raise(self):
        with self.assertRaises(ValueError):
            gusset.make_gusset_shell([(0, 0), (1, 1)], 6.0)
        with self.assertRaises(ValueError):
            gusset.make_gusset_shell([(0, 0), (10, 0), (10, 8), (0, 8)], 0.0)


class TestAnchorGussetsToModel(unittest.TestCase):
    def test_anchor_by_node_id_with_metadata(self):
        m = _synth_model()
        res = gusset_anchor.anchor_gussets_to_model(m, [{"node_id": "N_mid0", "face": "front"}])
        self.assertEqual(len(res["plates"]), 1)
        plate = res["plates"][0]
        self.assertEqual(plate["node_id"], "N_mid0")
        self.assertEqual(plate["thickness_mm"], 6.0)          # 默认板厚 6mm
        self.assertAlmostEqual(plate["dimensions_mm"][0], 140 * 1.2, delta=1e-6)  # 最大肢宽×1.2
        self.assertEqual(len(plate["associated_bars"]), 4)  # B_up0/B_dn0/B_hz0 + B_hz3(mid3→mid0)
        self.assertTrue(plate["mesh"].is_watertight)
        self.assertIn(plate["gusset"], m.components)          # 写回模型
        comp = m.components[plate["gusset"]]
        self.assertEqual(comp.kind, "gusset_plate")
        self.assertEqual(comp.properties["source"], "synthesized")

    def test_anchor_by_z_picks_nearest_node(self):
        m = _synth_model()
        res = gusset_anchor.anchor_gussets_to_model(m, [{"z_mm": 17500, "face": "side"}])
        self.assertEqual(len(res["plates"]), 1)
        self.assertTrue(res["plates"][0]["node_id"].startswith("N_mid"))

    def test_isolated_node_fails_gracefully(self):
        m = EngineeringModel(name="iso")
        m.add_component(Component(id="N1", name="N1", kind="tower_node",
                                  properties={"x": 0, "y": 0, "z": 1000}))
        res = gusset_anchor.anchor_gussets_to_model(m, [{"node_id": "N1"}])
        self.assertEqual(res["plates"], [])
        self.assertEqual(res["failed"][0]["reason"], "no_intersecting_bars")

    def test_backward_compatible_signatures(self):
        """主线程 detail 通路引用的签名冻结（只加函数不改已有）。"""
        self.assertEqual(
            str(inspect.signature(gusset_anchor.anchor_gusset_to_node)),
            "(model: 'EngineeringModel', gusset_cid: 'str', node_cid: 'str', *, anchor_origin: 'str' = 'manual') -> 'bool'")
        self.assertEqual(
            str(inspect.signature(gusset_anchor.auto_anchor_gussets)),
            "(model: 'EngineeringModel', overlay: 'Optional[str | Path | dict]' = None) -> 'Dict[str, Any]'")
        self.assertEqual(
            str(inspect.signature(gusset.add_gusset_to_model)),
            "(model: 'EngineeringModel', plate: 'GussetPlate') -> 'Component'")
        self.assertTrue(callable(gusset_anchor.anchor_gussets_to_model))


class TestGussetArtifact(unittest.TestCase):
    @unittest.skipUnless((SOLID_DIR / "gusset_attached.glb").exists(), "gusset_attached.glb 未生成")
    def test_artifact_glb_and_json(self):
        j = _glb_json(SOLID_DIR / "gusset_attached.glb")
        names = [n.get("name") for n in j["nodes"]]
        self.assertGreaterEqual(len(names), 3)               # ≥3 典型节点
        self.assertTrue(all(names), "GLB 节点必须全部具名")
        for m in j["meshes"]:
            attrs = m["primitives"][0]["attributes"]
            self.assertIn("POSITION", attrs)
            self.assertIn("NORMAL", attrs)                    # 缺法线 → three.js 纯黑坑
        meta = json.loads((SOLID_DIR / "gusset_attached.json").read_text(encoding="utf-8"))
        plates = meta.get("plates", meta if isinstance(meta, list) else [])
        zs = sorted(float(p.get("position_mm", [0, 0, p.get("z", 0)])[2]
                          if isinstance(p.get("position_mm"), list) else p.get("z", 0))
                    for p in plates)
        self.assertGreaterEqual(len(plates), 3)
        # 覆盖 ≥2 个高度带（塔身中段 15-25k 与塔头 30k+）
        bands = {("mid" if 15000 <= z < 30000 else "head" if z >= 30000 else "low") for z in zs}
        self.assertGreaterEqual(len(bands), 2, f"节点带覆盖不足: {zs}")
