#!/usr/bin/env python3
"""Generate the phase-4 bolt/tower/gusset assembly (degrades on missing parts)."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SHEET = ROOT / "web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json"
SOLID_DEFAULT = ROOT / "out/35A1-JC1-solid"


def _find_part(filename: str, out_dir: Path, fallback_dir):
    """T5：部件查找先本目录，再回退标准 solid 目录——修「文件明明存在却报
    missing/degraded」的相对路径坑（从不同 cwd 或自定义 --out-dir 运行时）。"""
    for base in (out_dir, fallback_dir):
        if base is None:
            continue
        p = Path(base) / filename
        if p.exists():
            return p
    return None


def _groups(sheet=SHEET):
    data = json.loads(Path(sheet).read_text(encoding="utf-8"))
    out = []
    for cid, comp in data.get("components", {}).items():
        if comp.get("kind") != "bolt_group":
            continue
        p = comp.get("properties", {})
        holes = [h[:2] for h in (p.get("holes") or []) if len(h) >= 2]
        if holes:
            out.append({"component_id": cid, "group_id": p.get("group_id", cid),
                        "holes": holes, **{k: p.get(k) for k in ("length_mm", "plate_thickness_mm")}})
    return sorted(out, key=lambda x: x["component_id"])


def _d1_anchor(out_dir: Path, fallback_dir=None):
    """T2 变换链终点：D1 节点板世界系（gusset_attached.json manifest）。

    链：detail local holes → 以孔心质心居中的板局部系 → 板世界
    position/normal（G 锚定产物）。manifest 缺失时回退样例系（原点 +Z）
    并在报告 degraded_anchor=True 如实记录——不假装锚定成功。
    """
    mf = _find_part("gusset_attached.json", out_dir, fallback_dir)
    if mf is None:
        return None
    try:
        meta = json.loads(mf.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    plates = meta.get("plates", [])
    d1 = next((p for p in plates if str(p.get("source", "")).upper() == "D1"), None)
    if not d1 or not d1.get("position_mm") or not d1.get("normal"):
        return None
    return {"node_id": d1.get("node_id"),
            "position_mm": [float(v) for v in d1["position_mm"]],
            "normal": [float(v) for v in d1["normal"]]}


def build(out_dir: Path, sheet=SHEET, fallback_dir=SOLID_DEFAULT):
    from traceability.connection.bolt_mesh import bolt_assembly_meshes
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = _groups(sheet)
    # T2：detail 局部孔位 → 孔心质心居中（与 build_detail_sample 同约定）
    # → D1 板世界系锚定（gusset_attached.json 的 position/normal）。
    anchor = _d1_anchor(out_dir, fallback_dir)
    allh = [h for g in groups for h in g["holes"]]
    cx = sum(h[0] for h in allh) / len(allh) if allh else 0.0
    cy = sum(h[1] for h in allh) / len(allh) if allh else 0.0
    center = anchor["position_mm"] if anchor else [0.0, 0.0, 0.0]
    normal = anchor["normal"] if anchor else (0.0, 0.0, 1.0)
    scene = trimesh.Scene()
    missing = []
    parts = {}
    for filename, label in (("solid_angle_tower.glb", "angle_tower"),
                            ("gusset_attached.glb", "gusset")):
        path = _find_part(filename, out_dir, fallback_dir)
        if path is None:
            missing.append(filename)
            continue
        loaded = trimesh.load(path, force="scene")
        count = 0
        if isinstance(loaded, trimesh.Scene):
            for name, geom in loaded.geometry.items():
                scene.add_geometry(geom, geom_name=f"{label}_{name}")
                count += 1
        else:
            scene.add_geometry(loaded, geom_name=label); count = 1
        parts[label] = count
    bolt_count = 0
    group_centers = []
    for g in groups:
        g2 = dict(g)
        g2["holes"] = [[h[0] - cx, h[1] - cy] for h in g["holes"]]
        mesh = bolt_assembly_meshes(g2, normal, center)
        mesh.visual.material = trimesh.visual.material.PBRMaterial(
            name="hot_dip_galvanized", metallicFactor=0.85, roughnessFactor=0.40,
            baseColorFactor=[170, 175, 182, 255])
        scene.add_geometry(mesh, geom_name=g["component_id"])
        bolt_count += len(g["holes"])
        bc = mesh.bounds
        group_centers.append({"group": g["component_id"],
                              "center_mm": [round(float((bc[0][i] + bc[1][i]) / 2), 1) for i in range(3)],
                              "bolt_count": len(g["holes"])})
    glb = out_dir / "assembly.glb"
    # Explicit include_normals is required: viewers otherwise render bolts black.
    scene.export(glb, file_type="glb", include_normals=True)
    report = {"assembly_glb": str(glb), "parts": parts, "bolt_groups": len(groups),
              "bolt_count": bolt_count, "material": {"name": "hot_dip_galvanized",
              "metallicFactor": 0.85, "roughnessFactor": 0.40}, "missing": missing,
              "degraded": bool(missing), "source_sheet": str(sheet),
              "anchor": {"plate": "gusset_D1" if anchor else None,
                         "node_id": anchor["node_id"] if anchor else None,
                         "plate_center_mm": center, "plate_normal": [float(v) for v in normal],
                         "detail_hole_centroid": [round(cx, 1), round(cy, 1)],
                         "degraded_anchor": anchor is None,
                         "note": "D1 polygon_local 为解析碎片（19×16mm）；螺栓群按孔群凸包"
                                 "工作区锚定在 D1 板世界系上（与 detail_sample 同一凸包修复口径）",
                         "group_centers": group_centers}}
    (out_dir / "assembly.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "out/35A1-JC1-solid"))
    ap.add_argument("--sheet", default=str(SHEET))
    args = ap.parse_args(argv)
    print(json.dumps(build(Path(args.out_dir), Path(args.sheet)), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
