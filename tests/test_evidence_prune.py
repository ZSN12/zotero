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


def _n3d(cid, x, y, z):
    return _Comp({"kind": "tower_node", "id": cid,
                  "properties": {"x": x, "y": y, "z": z}})


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

    def test_r5_removes_tip_platform_ring(self):
        """R5：tip_platform 模板环（terminal_pair_gen 域、无投影证据）→ 删。
        实测 ZC1 10 杆全 FP；JC1 生成器无角点证据时生成 0 杆不受影响。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "tip": _bar("tip", origin="terminal_pair_gen", role="HORIZ",
                       derived_from="tip_platform"),
            "pair": _bar("pair", origin="terminal_pair_gen", role="DIAG",
                         derived_from="4f_tps_xc_35800_36200_1"),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, {"evidence_prune": {
            "tip_platform_ring": True}})
        self.assertNotIn("tip", m.components)
        self.assertIn("pair", m.components,
                      "terminal_pair_gen 主力杆（R4 域）不受 R5 波及")
        self.assertEqual(rep["rules"]["R5_tip_platform_ring"], 1)

    def test_r5_spares_tip_ring_with_projection_refs(self):
        """R5 前置条件：带投影证据的 tip_platform 杆保留——若某塔型
        平台环真有 2D 证据（中心构型平台），规则不误杀。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "tip": _bar("tip", origin="terminal_pair_gen", role="HORIZ",
                        derived_from="tip_platform",
                        refs=[{"sheet_id": "S1"}]),
            "drawing_file": _drawing_file(),
        })
        apply_evidence_prune(m, {"evidence_prune": {
            "tip_platform_ring": True}})
        self.assertIn("tip", m.components)

    def test_r6_removes_leg_stitch_bridge(self):
        """R6：leg_chain_stitch 拼接桥全剪（refs 指向源线非自身证据，
        双塔实测 0/30 TP）。带 refs 的一并剪——豁免对插值语义无效。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "st_ref": _bar("st_ref", origin="leg_chain_stitch",
                           refs=[{"sheet_id": "S1"}]),
            "st_bare": _bar("st_bare", origin="leg_chain_stitch"),
            "marker": _bar("marker", origin="marker_synth",
                           refs=[{"sheet_id": "S1"}]),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, {"evidence_prune": {
            "leg_stitch_bridge": True}})
        self.assertNotIn("st_ref", m.components)
        self.assertNotIn("st_bare", m.components)
        self.assertIn("marker", m.components,
                      "带 refs 的 marker_synth 不受 R6 影响（R6 只管 stitch）")
        self.assertEqual(rep["rules"]["R6_leg_stitch_bridge"], 2)

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


    def _r8_cfg(self):
        """R8 测试用 overlay（与 ZC1 overlay 同构，家族名单由 overlay 给出）。"""
        return {"evidence_prune": {"panel_slot_nms": {
            "zero_tp_origins": ["derived_4face", "derived_parametric_base",
                                "marker_synth"],
            "dxf_origins": ["dxf_geom"],
            "mutex_origins": ["terminal_pair_gen",
                              "panel_template_completion"],
            "evidence_origins": ["dxf_geom"],
            "z_floor_mm": 4500.0, "level_gap_mm": 400.0,
            "snap_tol_mm": 800.0,
            "orphan": {
                "diag_span2_terminal_dz_min_mm": 1500.0,
                "diag_span2_panel_dz_max_mm": 1200.0,
                "diag_span1_panel_dz_max_mm": 1200.0,
                "horiz_k": 3, "horiz_dense_n": 4, "horiz_dense_k": 2,
            },
            "leg": {"panel_dz_max_mm": 1000.0, "same_segment_dedup": True},
        }}}

    def _bar3d(self, cid, origin, role, a, b, **extra):
        return _bar(cid, origin=origin, role=role, frm=a, to=b, **extra)

    def test_r8_disabled_by_default_zero_behavior(self):
        """红线：evidence_prune 开但无 panel_slot_nms 键 → R8 零行为。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "dpb": _bar("dpb", origin="derived_parametric_base"),
            "term": _bar("term", origin="terminal_pair_gen", role="DIAG"),
            "drawing_file": _drawing_file(),
        })
        # overlay 有 evidence_prune 键（其他规则关）但无 panel_slot_nms
        # → R8 不触发；dpb 虽是推断 origin，no_projection_evidence=False
        # 时无任何规则可删它
        rep = apply_evidence_prune(m, {"evidence_prune": {
            "no_projection_evidence": False}})
        self.assertTrue(rep["enabled"])
        self.assertNotIn("panel_slot_nms", rep)
        self.assertIn("dpb", m.components)
        self.assertIn("term", m.components)
        # panel_slot_nms 为空 dict → 机制开启但无家族名单 → 零删除
        rep2 = apply_evidence_prune(m, {"evidence_prune": {
            "panel_slot_nms": {}}})
        self.assertEqual(rep2["rules"], {})
        self.assertIn("term", m.components)

    def test_r8_zero_tp_origins_full_kill(self):
        """R8a：overlay 显式列出的零 TP 家族全量剪除；R8b dxf 关闭
        也剪但计独立规则名（同一杆只记先触发的一条）。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "g4": _bar("g4", origin="derived_4face"),
            "dpb": _bar("dpb", origin="derived_parametric_base"),
            "dxf": _bar("dxf", origin="dxf_geom", face="f", gen4=True,
                        refs=[{"sheet_id": "S"}]),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, self._r8_cfg())
        self.assertNotIn("g4", m.components)
        self.assertNotIn("dpb", m.components)
        self.assertNotIn("dxf", m.components, "ZC1 语义下 dxf 整族关闭")
        self.assertEqual(rep["rules"]["R8_zero_tp_origin"], 2)
        self.assertEqual(rep["rules"]["R8_dxf_quality_off"], 1)

    def test_r8_slot_mutex_keep_top_k_by_tier(self):
        """R8c：同槽位 mutex 家族竞争——高置信(tier 小)保、低置信砍。
        无坐标节点 → HORIZ/DIAG 分类 None → 不入槽（k0/k9 不动）。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        # 节点：level 表 = [0(z floor 以下)] 之上造两级 (z=5000, z=9000)
        comps = {
            "nA0": _n3d("nA0", 1000, 1000, 5000), "nA1": _n3d("nA1", 1000, 1000, 9000),
            "nB0": _n3d("nB0", -1000, 1000, 5000), "nB1": _n3d("nB1", -1000, 1000, 9000),
            "drawing_file": _drawing_file(),
        }
        # 同一 HORIZ 槽（z=5000, NS+ 面）：4 根模板杆，容量 k=3
        for i in range(3):
            comps[f"t{i}"] = self._bar3d(f"t{i}", "terminal_pair_gen", "HORIZ",
                                     "nA0", "nB0")
        comps["p3"] = self._bar3d("p3", "panel_template_completion", "HORIZ",
                             "nA0", "nB0")
        # 无坐标端点杆：不入槽、不参与竞争
        comps["k0"] = _bar("k0", origin="terminal_pair_gen", role="HORIZ",
                           frm="ghost", to="nA0")
        # 非 mutex 家族：不入槽
        comps["n9"] = self._bar3d("n9", "neck_brace_completion", "HORIZ",
                             "nA0", "nB0")
        m = _Model(comps)
        rep = apply_evidence_prune(m, self._r8_cfg())
        # 孤儿槽（无 dxf 证据）HORIZ：4 根候选 ≥ dense_n(4) → k=2；
        # 排序 (tier, cid)：两家族同 tier=2 → cid 字典序 p3 排最前 →
        # keep {p3, t0}，砍 t1/t2（机制性 tie-break，与离线校准一致）
        self.assertIn("p3", m.components)
        self.assertIn("t0", m.components)
        self.assertNotIn("t1", m.components)
        self.assertNotIn("t2", m.components)
        self.assertIn("k0", m.components)
        self.assertIn("n9", m.components)
        self.assertEqual(rep["rules"]["R8_slot_orphan"], 2)

    def test_r8_slot_mutex_evidenced_slot_keeps_top_k(self):
        """R8c：有证据（dxf 在槽内）的 HORIZ 槽 keep-K 容量 3。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        comps = {
            "nA0": _n3d("nA0", 1000, 1000, 5000),
            "nB0": _n3d("nB0", -1000, 1000, 5000),
            "drawing_file": _drawing_file(),
        }
        # dxf 证据杆占槽（自身被 R8b 关闭但槽已标记 evidenced）
        comps["ev"] = self._bar3d("ev", "dxf_geom", "HORIZ", "nA0", "nB0",
                             refs=[{"sheet_id": "S"}])
        for i in range(4):
            comps[f"t{i}"] = self._bar3d(f"t{i}", "terminal_pair_gen", "HORIZ",
                                    "nA0", "nB0")
        m = _Model(comps)
        rep = apply_evidence_prune(m, self._r8_cfg())
        # evidenced → k=3（不触发 dense 降 K），keep t0..t2，砍 t3
        for i in range(3):
            self.assertIn(f"t{i}", m.components)
        self.assertNotIn("t3", m.components)
        self.assertEqual(rep["rules"]["R8_slot_mutex"], 1)

    def test_r8_orphan_diag_dz_windows(self):
        """R8c：孤儿斜材 dz 节拍窗——span1 terminal 无条件保、
        panel dz≤1200 保；dz 超窗或节拍不符（跨两层 dz 仅 1000）砍。
        dz>5000 的长斜杆被 classify 归 LEG（结构属性分类，role 仅参考）。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        # level 表：z 5000/9000/13000（gap 400 切带互不粘连）
        comps = {
            "n0a": _n3d("n0a", 1000, 1000, 5000),
            "n0b": _n3d("n0b", -1000, 1000, 5000),
            "n1a": _n3d("n1a", 1000, 1000, 9000),
            "n2a": _n3d("n2a", 1000, 1000, 13000),
            "drawing_file": _drawing_file(),
        }
        # t_s2_ok：n0a→n2a dz=8000 → LEG 分类，terminal 不受 panel 窗口 → 保
        comps["t_s2_ok"] = self._bar3d("t_s2_ok", "terminal_pair_gen", "DIAG",
                                  "n0a", "n2a")
        # p_s2_bad：同 dz=8000 → LEG 分类，panel dz>1000 → 砍（R8_leg_
        # panel_dz_window——长斜杆归 LEG 治理是机制语义）
        comps["p_s2_bad"] = self._bar3d("p_s2_bad", "panel_template_completion",
                                   "DIAG", "n0b", "n2a")
        # p_s2_short：n1a→n2a（level 1→2，span=1）dz=4000>1200 → 砍
        comps["p_s2_short"] = self._bar3d("p_s2_short",
                                     "panel_template_completion", "DIAG",
                                     "n1a", "n2a")
        # span1 terminal：无条件保
        comps["t_s1_ok"] = self._bar3d("t_s1_ok", "terminal_pair_gen", "DIAG",
                                  "n0a", "n1a")
        # span1 panel dz=4000>1200 → 砍（R8_slot_orphan）
        comps["p_s1_bad"] = self._bar3d("p_s1_bad", "panel_template_completion",
                                  "DIAG", "n0b", "n1a")
        m = _Model(comps)
        rep = apply_evidence_prune(m, self._r8_cfg())
        self.assertIn("t_s2_ok", m.components)
        self.assertNotIn("p_s2_bad", m.components)
        self.assertNotIn("p_s2_short", m.components)
        self.assertIn("t_s1_ok", m.components)
        self.assertNotIn("p_s1_bad", m.components)
        self.assertEqual(rep["rules"]["R8_slot_orphan"], 2)
        self.assertEqual(rep["rules"]["R8_leg_panel_dz_window"], 1)

    def test_r8_leg_panel_dz_window_and_dedup(self):
        """R8e：panel LEG dz>窗口 砍；同棱同 z 段第二份模板拷贝去重。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        comps = {
            "lg0": _n3d("lg0", 1000, 1000, 5000),
            "lg1": _n3d("lg1", 1000, 1000, 15000),
            "lg2": _n3d("lg2", 1000, 1000, 5500),
            "drawing_file": _drawing_file(),
        }
        # panel LEG dz=10000 > 1000 → 砍（R8_leg_panel_dz_window）
        comps["leg_bad"] = self._bar3d("leg_bad", "panel_template_completion",
                                  "LEG", "lg0", "lg1")
        # terminal LEG dz=10000（同窗）→ 不受 panel 窗口约束
        comps["leg_t"] = self._bar3d("leg_t", "terminal_pair_gen", "LEG",
                                "lg0", "lg1")
        # 同棱 (1,1) 同 z 段 5000-5500 两份（dz=500 在窗口内不触发 R8e 窗口）
        # → 去重保先见者：sorted(bars) 字典序 d1<d2 → 保 d1 砍 d2
        comps["d1"] = self._bar3d("d1", "terminal_pair_gen", "LEG", "lg0", "lg2")
        comps["d2"] = self._bar3d("d2", "panel_template_completion", "LEG",
                             "lg0", "lg2")
        m = _Model(comps)
        rep = apply_evidence_prune(m, self._r8_cfg())
        self.assertNotIn("leg_bad", m.components)
        self.assertIn("leg_t", m.components)
        self.assertIn("d1", m.components)
        self.assertNotIn("d2", m.components)
        self.assertEqual(rep["rules"]["R8_leg_panel_dz_window"], 1)
        self.assertEqual(rep["rules"]["R8_leg_same_segment"], 1)

    def test_r8_provenance_and_report(self):
        """R8 证据链：被剪杆写 pruned_by=R8_*；panel_slot_nms 报告落 drawing_file。"""
        from traceability.solve.evidence_prune import apply_evidence_prune
        m = _Model({
            "dpb": _bar("dpb", origin="derived_parametric_base",
                        frm="x1", to="x2"),
            "drawing_file": _drawing_file(),
        })
        rep = apply_evidence_prune(m, self._r8_cfg())
        df = m.components["drawing_file"]
        ps = df["properties"]["evidence_prune_report"]["panel_slot_nms"]
        self.assertEqual(ps["levels"], 0, "无坐标节点 → 空层表")
        self.assertEqual(rep["removed_ids"], ["dpb"])
        self.assertEqual(rep["rules"]["R8_zero_tp_origin"], 1)


if __name__ == "__main__":
    unittest.main()
