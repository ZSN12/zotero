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


def build(out_dir: Path, sheet=SHEET):
    from traceability.connection.bolt_mesh import bolt_assembly_meshes
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = _groups(sheet)
    # The sample coordinate system is centered at the hole centroid, as in
    # build_detail_sample.py; local hole coordinates are centered by the mesh
    # generator and plate_center is therefore the world origin.
    center = [0.0, 0.0, 0.0]
    scene = trimesh.Scene()
    missing = []
    parts = {}
    for filename, label in (("solid_angle_tower.glb", "tower"), ("gusset_attached.glb", "gusset")):
        path = out_dir / filename
        if not path.exists():
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
    for g in groups:
        # local plate coordinates are centered; z=0 is the plate center.
        mesh = bolt_assembly_meshes(g, (0, 0, 1), center)
        mesh.visual.material = trimesh.visual.material.PBRMaterial(
            name="hot_dip_galvanized", metallicFactor=0.85, roughnessFactor=0.40,
            baseColorFactor=[170, 175, 182, 255])
        scene.add_geometry(mesh, geom_name=g["component_id"])
        bolt_count += len(g["holes"])
    glb = out_dir / "assembly.glb"
    # Explicit include_normals is required: viewers otherwise render bolts black.
    scene.export(glb, file_type="glb", include_normals=True)
    report = {"assembly_glb": str(glb), "parts": parts, "bolt_groups": len(groups),
              "bolt_count": bolt_count, "material": {"name": "hot_dip_galvanized",
              "metallicFactor": 0.85, "roughnessFactor": 0.40}, "missing": missing,
              "degraded": bool(missing), "source_sheet": str(sheet)}
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
