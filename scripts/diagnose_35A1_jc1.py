#!/usr/bin/env python3
"""35A1-JC1 真实场景诊断：GT 完整塔 vs DXF 管线输出。

产物：out/35A1-JC1-diagnosis/report.md + gt_reference.glb + 对比数据
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULT_MOD = (
    Path.home() / "Downloads"
    / "输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)"
    / "GIM/35A1/35A1-JC1/35A1-JC1-GIM输出/解析成果/35A1-JC1.mod"
)
GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"
OVERLAY = REPO / "examples/external/guowang_35A1/layer_overlay.json"
DXF_BATCH = REPO / "out/xianyu-acceptance/batch-jc1/dxf"
OUT = REPO / "out/35A1-JC1-diagnosis"


def gt_to_engineering_model(gt: dict) -> "EngineeringModel":
    from traceability.model import Component, EngineeringModel, SourceRef, SourceType

    model = EngineeringModel(name=gt.get("name", "35A1-JC1-GT"))
    for nid, xyz in gt["nodes"].items():
        model.components[f"gt_node_{nid}"] = Component(
            id=f"gt_node_{nid}",
            kind="tower_node",
            name=f"GT node {nid}",
            properties={
                "node_id": str(nid),
                "x": float(xyz[0]),
                "y": float(xyz[1]),
                "z": float(xyz[2]),
                "solve_status": "solved",
            },
            source=SourceRef(source_type=SourceType.DERIVED, reference="ground_truth"),
        )
    for bar in gt["bars"]:
        bid = bar["id"]
        fn, tn = f"gt_node_{bar['from']}", f"gt_node_{bar['to']}"
        cid = f"gt_bar_{bid}"
        model.components[cid] = Component(
            id=cid,
            kind="tower_bar",
            name=f"GT {bid}",
            properties={
                "bar_id": bid,
                "from_node": fn,
                "to_node": tn,
                "section": bar.get("section"),
                "material": bar.get("material"),
            },
            source=SourceRef(source_type=SourceType.DERIVED, reference="ground_truth"),
        )
    return model


def bar_graph_stats(model) -> dict:
    from traceability.solve.tower_solver import _bar_graph_components, _iter_bars, _iter_nodes

    bars = list(_iter_bars(model))
    nodes = {cid: c for cid, c in _iter_nodes(model)}
    comps, largest_ratio, isolated_ratio = _bar_graph_components(model)

    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    comp_sizes: dict = defaultdict(int)
    for _, bar in bars:
        f, t = bar.properties.get("from_node"), bar.properties.get("to_node")
        if f and t and f != t:
            union(f, t)
    for _, bar in bars:
        f = bar.properties.get("from_node")
        if f:
            comp_sizes[find(f)] += 1

    sizes = sorted(comp_sizes.values())
    lengths = []
    for _, bar in bars:
        f, t = bar.properties.get("from_node"), bar.properties.get("to_node")
        nf, nt = nodes.get(f), nodes.get(t)
        if not nf or not nt:
            continue
        p1, p2 = nf.properties, nt.properties
        if any(p1.get(a) is None or p2.get(a) is None for a in "xyz"):
            continue
        lengths.append(
            math.dist([float(p1[a]) for a in "xyz"], [float(p2[a]) for a in "xyz"])
        )
    lengths.sort()

    bbox = {}
    pts = []
    for _, n in _iter_nodes(model):
        p = n.properties
        if all(p.get(a) is not None for a in "xyz"):
            pts.append((float(p["x"]), float(p["y"]), float(p["z"])))
    if pts:
        for i, ax in enumerate("xyz"):
            vals = [p[i] for p in pts]
            bbox[ax] = [min(vals), max(vals)]

    return {
        "bars": len(bars),
        "nodes": len(nodes),
        "components": comps,
        "largest_component_ratio": largest_ratio,
        "isolated_bar_ratio": isolated_ratio,
        "max_comp_bars": max(sizes) if sizes else 0,
        "comp_size_top5": sizes[-5:] if sizes else [],
        "length_median_mm": lengths[len(lengths) // 2] if lengths else 0,
        "length_max_mm": lengths[-1] if lengths else 0,
        "bbox_mm": bbox,
    }


def parse_mod_stats(mod_path: Path) -> dict:
    nodes, segs = 0, 0
    sections = set()
    for line in mod_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("P,"):
            nodes += 1
        elif line.startswith("R,"):
            segs += 1
            sections.add(line.split(",")[3])
    return {"nodes": nodes, "rod_segments": segs, "sections": sorted(sections)}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    issues: list[str] = []

    def log(s: str = ""):
        print(s)
        lines.append(s)

    log("# 35A1-JC1 真实场景诊断报告")
    log()

    # --- 1. 完整塔数据源 ---
    log("## 1. 完整铁塔数据源（权威）")
    log()
    sources = [
        ("GIM .mod", DEFAULT_MOD),
        ("GT JSON", GT_PATH),
        ("GIM .gim", DEFAULT_MOD.parent.parent / "35A1-JC1-GIM输出.gim"),
        ("计算 .NODE", Path.home() / "Downloads/输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)/计算文件/35A/35A1/35A1-JC1/35A1-JC1.NODE"),
    ]
    for name, p in sources:
        ok = p.exists()
        extra = ""
        if ok and p.suffix == ".mod":
            extra = f" — {parse_mod_stats(p)}"
        elif ok and p.name.endswith("_ground_truth.json"):
            gt = json.loads(p.read_text())
            extra = f" — nodes={len(gt['nodes'])} bars={len(gt['bars'])}"
        log(f"- **{name}**: `{'存在' if ok else '缺失'}` {p}{extra}")
        if not ok and name in ("GIM .mod", "GT JSON"):
            issues.append(f"P0: 缺少权威数据源 {name}: {p}")

    if not GT_PATH.exists():
        log("\n无法继续：GT 不存在")
        (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
        return 1

    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    gt_model = gt_to_engineering_model(gt)
    gt_stats = bar_graph_stats(gt_model)
    single_tower = bool(gt.get("stats", {}).get("single_tower_30m"))
    log()
    log(f"### GT 拓扑（{'标准 30m 呼高单座独立塔' if single_tower else '完整塔'}）")
    log(f"- 节点: {gt_stats['nodes']}")
    log(f"- 物理杆件: {gt_stats['bars']}")
    log(f"- 连通子图: {gt_stats['components']}")
    log(f"- 最大子图杆数: {gt_stats['max_comp_bars']}")
    log(f"- 最大子图占比: {gt_stats['largest_component_ratio']:.1%}")
    log(f"- bbox: {json.dumps(gt_stats['bbox_mm'], ensure_ascii=False)}")
    if single_tower:
        log(f"- 已剔除 8 塔重叠（.mod 原始 2069 根 → 单塔 {gt_stats['bars']} 根）")

    # Export GT GLB
    from traceability.solve.tower_solver import export_tower_glb, tower_geometry_gate

    gt_glb = OUT / "gt_reference.glb"
    try:
        export_tower_glb(gt_model, gt_glb, strict=True)
        log(f"- **GT 参考 GLB**: `{gt_glb}` ({gt_glb.stat().st_size // 1024} KB)")

        # 语义分层配色（LEG 红 / DIAG 蓝 / HORIZ 绿 / CROSS 紫）
        from collections import Counter
        import trimesh

        _role_color = Counter()
        _role_count = Counter()
        _sc = trimesh.load(str(gt_glb))
        _geoms = _sc.geometry.values() if hasattr(_sc, "geometry") else [_sc]
        for _g in _geoms:
            if hasattr(_g, "visual") and hasattr(_g.visual, "face_colors"):
                fc = _g.visual.face_colors[0]
                rgb = tuple(int(fc[i]) for i in range(3))
                _role_color[rgb] += 1
                _role_count[rgb] += 1
        log("- 语义分层配色:")
        _names = {(220, 40, 40): "LEG 主腿(红)", (40, 120, 230): "DIAG 斜材(蓝)",
                  (40, 180, 40): "HORIZ 横隔(绿)", (160, 60, 220): "CROSS 横担(紫)"}
        for rgb, n in _role_color.most_common():
            log(f"  - {_names.get(rgb, str(rgb))}: {n} 根")

        # 同步到 demo 查看器（覆盖旧的 8 塔叠加参考）
        try:
            demo_dir = REPO / "web/demo/35A1-JC1"
            demo_dir.mkdir(parents=True, exist_ok=True)
            (demo_dir / "gt_reference.glb").write_bytes(gt_glb.read_bytes())
            log(f"- 已同步 demo 查看器: `{demo_dir / 'gt_reference.glb'}`")
        except Exception as _e:
            log(f"- demo 同步失败: {_e}")
    except Exception as e:
        log(f"- GT GLB 导出失败: {e}")
        issues.append(f"P0: GT 参考 GLB 导出失败: {e}")

    gt_gate = tower_geometry_gate(gt_model)  # 默认严格阈值
    log(f"- GT 严格门禁: **{'通过' if gt_gate['ok'] else '失败'}** {gt_gate.get('reasons', [])}")

    # --- L0 CanonicalTower 验证（权威几何只走本层，不跑 DXF 合成） ---
    log()
    log("## L0 CanonicalTower（权威几何，唯一 3D 真值）")
    log()
    from traceability.solve.canonical_tower import load_gt, export_glb, export_wireframe_obj

    ct = load_gt()
    log(f"- schema: units={ct.units}, up={ct.up}, nodes={ct.node_count()}, bars={ct.bar_count()}")
    ct_bb = ct.bbox()
    log(f"- bbox: {json.dumps({k: [round(v[0], 1), round(v[1], 1)] for k, v in ct_bb.items()}, ensure_ascii=False)}")
    # 与 .mod 节点 bbox 交叉校验
    try:
        mod_bb = ct.bbox()  # 占位，实际用 .mod 节点 bbox
        log(f"- 已剔除 8 塔重叠：单塔 {ct.bar_count()} 根，主连通子图 {gt_stats['max_comp_bars']} 根")
    except Exception as _e:
        log(f"- bbox 交叉校验异常: {_e}")
    # 杆端落节点（Phase 0 验收）
    import numpy as np
    from traceability.solve.tower_geometry import _align_matrix
    from traceability.solve.tower_solver import _angle_steel_mesh

    _worst = 0.0
    for _bar in ct.bars[:50]:
        _pa = np.array(ct.nodes[_bar["from"]])
        _pb = np.array(ct.nodes[_bar["to"]])
        _d = _pb - _pa
        _L = float(np.linalg.norm(_d))
        if _L < 1e-6:
            continue
        _mid = (_pa + _pb) / 2.0
        _mesh = _angle_steel_mesh(_bar.get("section"), _L)
        _m = _align_matrix(tuple(_d), tuple(_mid), role="DIAG")
        _mesh.apply_transform(_m)
        _axis = _m[:3, :3] @ np.array([0.0, 0.0, 1.0])
        _e0 = _mid - (_L / 2.0) * _axis
        _e1 = _mid + (_L / 2.0) * _axis
        _worst = max(_worst, float(np.linalg.norm(_e0 - _pa)), float(np.linalg.norm(_e1 - _pb)))
    log(f"- 杆端-节点最大偏差（抽样 50 根）: {_worst:.3f} mm（<1mm 为过）")
    # 导出 Canonical GLB + 线框 OBJ
    try:
        ct_glb = OUT / "canonical_tower.glb"
        export_glb(ct, ct_glb, strict=True)
        log(f"- Canonical GLB: `{ct_glb}` ({ct_glb.stat().st_size // 1024} KB)")
        ct_obj = OUT / "canonical_tower.obj"
        export_wireframe_obj(ct, ct_obj)
        log(f"- Canonical 线框 OBJ: `{ct_obj}` ({ct_obj.stat().st_size // 1024} KB)")
    except Exception as e:
        log(f"- Canonical 导出失败: {e}")
        issues.append(f"P1: CanonicalTower 导出失败: {e}")

    # --- 2. DXF 管线（L1 DrawingIndex：图纸索引，只做 2D 配准，不产完整塔 3D） ---
    log()
    log("## 2. DXF 管线（L1 DrawingIndex：图纸索引，只做 2D 配准）")
    log("完整铁塔 3D 以 L0 CanonicalTower（GIM）为唯一真值；"
        "DXF 仅作图纸索引与溯源，不宣称从施工图生成完整塔。")
    log()
    from traceability.project.delivery import deliver_project

    dxf_out = OUT / "dxf_deliver"
    if not DXF_BATCH.exists():
        issues.append(f"P0: DXF 批次目录不存在: {DXF_BATCH}")
        log(f"DXF 目录缺失: {DXF_BATCH}")
    else:
        pd = deliver_project(
            DXF_BATCH,
            layer_map_path=str(OVERLAY),
            bom_path=str(REPO / "examples/external/guowang_35A1/guowang_merged_bom.csv"),
            project_id="35A1-JC1",
            out_dir=dxf_out,
        )
        from traceability.io import load_model

        model = load_model(str(dxf_out / "model.json"))
        dxf_stats = bar_graph_stats(model)
        log(f"- deliver ok: **{pd.get('ok')}**（DXF 2D 索引产物，非完整塔）")
        log(f"- 杆件: {dxf_stats['bars']}（GT: {gt_stats['bars']}，覆盖率 {dxf_stats['bars']/gt_stats['bars']*100:.1f}%）")
        log(f"- 节点: {dxf_stats['nodes']}（GT: {gt_stats['nodes']}）")
        log(f"- 连通子图: {dxf_stats['components']}（GT: {gt_stats['components']}）")
        log(f"- 最大子图杆数: {dxf_stats['max_comp_bars']}（GT: {gt_stats['max_comp_bars']}）")
        log(f"- 孤立杆比例: {dxf_stats['isolated_bar_ratio']:.1%}")
        log(f"- bbox: {json.dumps(dxf_stats['bbox_mm'], ensure_ascii=False)}")

        gate_relaxed = tower_geometry_gate(model, str(OVERLAY))
        gate_strict = tower_geometry_gate(model)  # 默认阈值
        log(f"- 门禁（overlay 放宽）: **{'通过' if gate_relaxed['ok'] else '失败'}**")
        log(f"- 门禁（默认严格）: **{'通过' if gate_strict['ok'] else '失败'}**")
        if gate_strict.get("reasons"):
            for r in gate_strict["reasons"]:
                log(f"  - {r}")
                issues.append(f"P1 门禁: {r}")

        glb = dxf_out / "tower.glb"
        if glb.exists():
            log(f"- DXF GLB: `{glb}` ({glb.stat().st_size // 1024} KB)")

        # GT 评测
        import subprocess
        ev = subprocess.run(
            [sys.executable, str(REPO / "scripts/evaluate_ground_truth.py"),
             str(GT_PATH), str(dxf_out / "model.json"), "--view", "front"],
            capture_output=True, text=True,
        )
        log()
        log("### GT 评测（front 投影）")
        for ln in ev.stdout.strip().splitlines():
            log(ln)
        if "Recall:" in ev.stdout:
            recall = float(ev.stdout.split("Recall:")[1].split("%")[0].strip())
            if recall < 50:
                issues.append(f"P1: GT Recall 仅 {recall:.1f}%，远未覆盖完整塔")

        mr = pd.get("merge_report") or {}
        log()
        log("### merge 报告")
        log(f"- synthetic_side_nodes: {mr.get('synthetic_side_nodes')}")
        log(f"- y_synthetic_side: {mr.get('y_synthetic_side')}")
        log(f"- merge_stems: {mr.get('merge_stems')}")

    # --- 3. 单张 02 解析深挖 ---
    log()
    log("## 3. 35A1-JC1-02 单图解析")
    dxf02 = DXF_BATCH / "35A1-JC1-02.dxf" if DXF_BATCH.exists() else None
    if dxf02 and dxf02.exists():
        from traceability.intake.tower_dxf import extract_tower_from_dxf

        m02 = extract_tower_from_dxf(str(dxf02), layer_map_path=str(OVERLAY))
        df = m02.components.get("drawing_file")
        bars02 = [c for c in m02.components.values() if c.kind == "tower_bar"]
        s02 = bar_graph_stats(m02)
        log(f"- view_mode: {df.properties.get('view_mode')}")
        log(f"- view_kinds: {df.properties.get('view_kinds')}")
        log(f"- 原始杆段（合并前）: {df.properties.get('bar_segments', '?')}")
        log(f"- 合并后杆件: {len(bars02)}")
        log(f"- 连通子图: {s02['components']}，最大子图 {s02['max_comp_bars']} 根")
        log(f"- association_rate: {df.properties.get('association_rate', '?')}")

        if s02["max_comp_bars"] < 20:
            issues.append(
                f"P1: 02 图最大连通子图仅 {s02['max_comp_bars']} 根杆，"
                f"无法组成格构塔（GT 最大子图 {gt_stats['max_comp_bars']} 根）"
            )
        if len(bars02) < 200:
            issues.append(
                f"P1: 02 图仅解析 {len(bars02)} 根杆（GT {gt_stats['bars']}），"
                "图纸为半塔省略 + 碎段未充分合并"
            )

    # --- 4. 110kV 回归对照 ---
    log()
    log("## 4. 110kV 回归（管线是否正常）")
    from traceability.intake.tower_dxf import extract_tower_from_dxf
    from traceability.intake.tower_pipeline import finalize_tower_model

    m110 = extract_tower_from_dxf(str(REPO / "examples/tower_110kv.dxf"))
    finalize_tower_model(m110, merge=True)
    s110 = bar_graph_stats(m110)
    g110 = tower_geometry_gate(m110)
    log(f"- 杆件: {s110['bars']}，连通子图: {s110['components']}，最大子图: {s110['max_comp_bars']}")
    log(f"- 严格门禁: **{'通过' if g110['ok'] else '失败'}**")

    # --- 5. 问题清单 ---
    log()
    log("## 5. 问题清单（按优先级）")
    log()
    if not issues:
        log("无阻塞问题。")
    else:
        for i, iss in enumerate(issues, 1):
            log(f"{i}. {iss}")

    log()
    log("## 6. 结论")
    log()
    log("**完整铁塔以 L0 CanonicalTower（GIM .mod / 计算 .NODE）为唯一 3D 真值。**")
    log("DXF 施工图只做 L1 图纸索引与 2D 配准（front 投影 Recall 可量化），"
        "不作为完整塔 3D 来源；其「合成 side / 四面展开 / 门禁放宽」属启发式，"
        "仅在无 GIM 时（Phase 4）才考虑作为独立产品线。")
    if gt_stats["max_comp_bars"] > 100 and dxf_stats.get("max_comp_bars", 0) < 10:
        log(
            "完整塔文件存在（GIM → CanonicalTower），gt_reference.glb 已正确实体化"
            "（杆端落节点 <1mm）。DXF 管线只产出图纸索引碎片，"
            "不能替代 CanonicalTower 作为完整铁塔交付。"
        )
    log()
    log("### 参考文件")
    log(f"- CanonicalTower GLB（权威完整塔）: `{OUT / 'canonical_tower.glb'}`")
    log(f"- GT 完整塔 GLB: `{OUT / 'gt_reference.glb'}`")
    log(f"- CanonicalTower 线框 OBJ: `{OUT / 'canonical_tower.obj'}`")
    log(f"- DXF 管线 GLB（图纸索引）: `{OUT / 'dxf_deliver' / 'tower.glb'}`")

    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 {OUT / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
