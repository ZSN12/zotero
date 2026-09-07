# -*- coding: utf-8 -*-
"""阶段二：证据置信分层剪枝器（evidence_prune）回归测试。

背景（2026-09-07 双塔逐源归因实测，ZC1/JC1）：
  * R1 无投影证据纯推断杆：ZC1 砍 112（dtd 108 + boundary 4，全零 TP）；
    JC1 只砍 17（leg_chain_stitch 3 + boundary 14，其 marker_synth 78 根
    全带 projection_refs 天然豁免 15 TP）→ JC1 红线 TP=1067 精确保持。
  * R2 dxf_geom 四面镜像：face∈b/l/r 且 generated_4face。
  * 红线纪律：默认（overlay 无 evidence_prune 键）零行为变化；
    被剪杆写 pruned_by provenance；审计报告落 drawing_file.properties。
"""
import unittest


class _Comp(dict):
    @property
    def kind(self):
        return self["kind"]

    @property
    def properties(self):
        return self["properties"]


class _Model:
    def __init__(self, comps):
        self.components = comps


def _bar(cid, origin="dxf_geom", face=None, gen4=False, refs=None,
         role="DIAG", frm="n1", to="n2", **extra):
    props = {
        "geometry_origin": origin,
        "face": face,
        "generated_4face": gen4,
        "projection_refs": refs or [],
        "role": role,
        "from_node": frm,
        "to_node": to,
    }
    props.update(extra)
    return _Comp({"kind": "tower_bar", "id": cid, "properties": props})


def _node(cid):
    return _Comp({"kind": "tower_node", "id": cid, "properties": {"z": 0.0}})


def _drawing_file():
    return _Comp({"kind": "drawing_file", "id": "drawing_file", "properties": {}})


class EvidencePruneTest(unittest.TestCase):
    def test_disabled_by_default_zero_behavior(self):
        """红线：overlay 无 evidence_prune 键 → 一根不删。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "b1": _bar("b1", origin="diagonal_topology_reconstructed"),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, {})
        self.assertFalse(rep["enabled"])
        self.assertIn("b1", m.components)

    def test_r1_removes_inference_bar_without_projection_refs(self):
        """R1：推断 origin 且无投影证据 → 删，并写 pruned_by。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "b1": _bar("b1", origin="diagonal_topology_reconstructed"),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, {"evidence_prune": {
            "no_projection_evidence": True}})
        self.assertNotIn("b1", m.components)
        self.assertEqual(rep["rules"]["R1_no_projection_evidence"], 1)

    def test_r1_keeps_inference_bar_with_projection_refs(self):
        """R1 前置条件：带 projection_refs 的推断杆保留（JC1 marker_synth
        15 TP 全带 refs —— 两塔分流靠证据字段，非按塔枚举）。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "b1": _bar("b1", origin="marker_synth",
                       refs=[{"sheet_id": "S1"}]),
            "drawing_file": _drawing_file(),
        })
        apply_evidence_prune(m, {"evidence_prune": {
            "no_projection_evidence": True}})
        self.assertIn("b1", m.components)

    def test_r1_spares_template_origins(self):
        """R1 域不含模板补全层（terminal_pair_gen / panel_template_
        completion —— 双塔 TP 主力，归 R4 配额管理）。"""
        from traceability.solve.evidence_prune import INFERENCE_ORIGINS
        self.assertNotIn("terminal_pair_gen", INFERENCE_ORIGINS)
        self.assertNotIn("panel_template_completion", INFERENCE_ORIGINS)
        self.assertNotIn("derived_parametric_base", INFERENCE_ORIGINS)

    def test_r2_removes_dxf_mirror_face(self):
        """R2：dxf_geom 的 b/l/r 镜像杆删除，front 直读保留。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "f": _bar("f", origin="dxf_geom", face="f", gen4=True,
                      refs=[{"sheet_id": "S"}]),
            "b": _bar("b", origin="dxf_geom", face="b", gen4=True,
                      refs=[{"sheet_id": "S"}]),
            "l": _bar("l", origin="dxf_geom", face="l", gen4=True,
                      refs=[{"sheet_id": "S"}]),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, {"evidence_prune": {"mirror_face_dxf": True}})
        self.assertIn("f", m.components)
        self.assertNotIn("b", m.components)
        self.assertNotIn("l", m.components)
        self.assertEqual(rep["rules"]["R2_mirror_face_dxf"], 2)

    def test_r2_spares_non_generated_mirrors(self):
        """R2 只砍 generated_4face 衍生镜像；非展开生成的 b/l/r 杆不动。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "b": _bar("b", origin="dxf_geom", face="b", gen4=False,
                      refs=[{"sheet_id": "S"}]),
            "drawing_file": _drawing_file(),
        })
        apply_evidence_prune(m, {"evidence_prune": {"mirror_face_dxf": True}})
        self.assertIn("b", m.components)

    def test_r3_dangling_inference_bar(self):
        """R3：两端度=1 的无证据推断杆（阶段三孤儿判据，域=R1）。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        # 孤儿杆：独立节点对，无其他杆经过
        m = _Model({
            "orphan": _bar("orphan", origin="marker_synth",
                           frm="o1", to="o2"),
            "n1": _node("o1"), "n2": _node("o2"),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, {"evidence_prune": {
            "dangling_degree1": True}})
        self.assertNotIn("orphan", m.components)
        self.assertEqual(rep["rules"].get("R3_dangling_degree1", 0) + 1, 2)

    def test_r4_bom_quota_prunes_low_confidence_overage(self):
        """R4：分段配额超编，低置信（inference）先砍，高置信保留。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        bars = {
            "drawing_file": _drawing_file(),
        }
        # 07 段 4 根 LEG：1 直读 + 3 推断（推断里 2 根悬空）
        bars["rec"] = _bar("rec", origin="dxf_geom", face="f", gen4=True,
                           role="LEG", refs=[{"sheet_id": "S"}],
                           source_file="35A2-ZC1-07", evidence_status="recognized")
        for i in range(3):
            bars[f"inf{i}"] = _bar(
                f"inf{i}", origin="derived_parametric_base",
                role="LEG", source_file="35A2-ZC1-07",
                frm=f"o{i}a", to=f"o{i}b")
        m = _Model(bars)
        rep = apply_evidence_prune(m, {
            "evidence_prune": {
                "bom_quota": True,
                "segment_role_quota": {"07": 2},
            }})
        self.assertIn("rec", m.components, "直读杆必须保留")
        n_removed = sum(1 for k in m.components if k.startswith("inf"))
        self.assertEqual(n_removed, 1, "超编 2 根（4→cap2），砍 2 根推断、留 1")
        self.assertEqual(rep["rules"]["R4_bom_quota"], 2)

    def test_report_and_provenance_written(self):
        """证据链铁律：被剪杆写 pruned_by；报告落 drawing_file。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "b1": _bar("b1", origin="boundary_leg_bridge"),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, {"evidence_prune": {
            "no_projection_evidence": True}})
        df = m.components["drawing_file"]
        self.assertIn("evidence_prune_report", df["properties"])
        self.assertEqual(df["properties"]["evidence_prune_report"]["n_removed"], 1)
        self.assertEqual(rep["removed_ids"], ["b1"])


if __name__ == "__main__":
    unittest.main()
