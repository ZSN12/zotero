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
    log()
    log("### GT 拓扑（完整塔）")
    log(f"- 节点: {gt_stats['nodes']}")
    log(f"- 物理杆件: {gt_stats['bars']}")
    log(f"- 连通子图: {gt_stats['components']}")
    log(f"- 最大子图杆数: {gt_stats['max_comp_bars']}")
    log(f"- bbox: {json.dumps(gt_stats['bbox_mm'], ensure_ascii=False)}")

    # Export GT GLB
    from traceability.solve.tower_solver import export_tower_glb, tower_geometry_gate

    gt_glb = OUT / "gt_reference.glb"
    try:
        export_tower_glb(gt_model, gt_glb, strict=True)
        log(f"- **GT 参考 GLB**: `{gt_glb}` ({gt_glb.stat().st_size // 1024} KB)")
    except Exception as e:
        log(f"- GT GLB 导出失败: {e}")
        issues.append(f"P0: GT 参考 GLB 导出失败: {e}")

    gt_gate = tower_geometry_gate(gt_model)  # 默认严格阈值
    log(f"- GT 严格门禁: **{'通过' if gt_gate['ok'] else '失败'}** {gt_gate.get('reasons', [])}")

    # --- 2. DXF 管线 ---
    log()
    log("## 2. DXF 管线输出（咸鱼 50 张 + overlay）")
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
        log(f"- deliver ok: **{pd.get('ok')}**")
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
    if gt_stats["max_comp_bars"] > 100 and dxf_stats.get("max_comp_bars", 0) < 10:
        log(
            "**完整铁塔文件存在**（GIM .mod → GT JSON），可导出参考 GLB。"
            "DXF 管线目前只能生成**尺寸正确但拓扑碎裂的半塔片段**，"
            "不能替代权威 GT 作为完整铁塔交付。"
        )
    log()
    log("### 参考文件")
    log(f"- GT 完整塔 GLB: `{OUT / 'gt_reference.glb'}`")
    log(f"- DXF 管线 GLB: `{OUT / 'dxf_deliver' / 'tower.glb'}`")

    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入 {OUT / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
