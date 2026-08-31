#!/usr/bin/env python3
"""Generate a LOD3 gusset plate and solid bolt sample from detail sheet 03."""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHEET = REPO_ROOT / "web/demo/35A1-JC1/latest_deliver/sheets/35A1-JC1-03.json"
DEFAULT_OUT_DIR = REPO_ROOT / "out/35A1-JC1-lod-samples"

# Reuse the repository's audited pure-Python hull, offset and extrusion helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_detail_sample import (  # noqa: E402
    convex_hull,
    extrude_polygon_mesh,
    offset_convex_ccw,
    parse_detail_sheet,
)


def verify_glb(path: Path) -> dict:
    """Validate the minimal named, lit-mesh contract required by GLTFLoader."""
    data = Path(path).read_bytes()
    assert len(data) >= 20 and data[:4] == b"glTF", "不是 GLB 二进制"
    version, total_length = struct.unpack_from("<II", data, 4)
    assert version == 2, f"GLB version 必须为 2，实际 {version}"
    assert total_length == len(data), "GLB 文件长度头不匹配"
    json_length, chunk_type = struct.unpack_from("<II", data, 12)
    assert chunk_type == 0x4E4F534A, "首 chunk 不是 JSON"
    document = json.loads(data[20:20 + json_length].decode("utf-8"))
    nodes = document.get("nodes", [])
    assert nodes, "GLB 没有 nodes"
    assert all(node.get("name") for node in nodes), "GLB node 必须全部具名"
    primitives = [primitive for mesh in document.get("meshes", [])
                  for primitive in mesh.get("primitives", [])]
    assert primitives, "GLB 没有 mesh primitives"
    has_normal = all("POSITION" in p.get("attributes", {}) and
                     "NORMAL" in p.get("attributes", {}) for p in primitives)
    assert has_normal, "每个 primitive 必须含 POSITION 和 NORMAL"
    return {"node_count": len(nodes), "named": True, "has_normal": True}


def generate(sheet_path: Path, out_dir: Path, verify: bool = False) -> dict:
    import trimesh

    detail = parse_detail_sheet(Path(sheet_path))
    groups = detail["groups"]
    all_holes = [(x, y) for group in groups for x, y in group["holes"]]
    if not all_holes:
        raise ValueError("详图页没有可用孔心")

    # Keep original local coordinates: both repaired outline and bolt solids share them.
    seed = list(detail["polygon_local"]) + all_holes
    hull = convex_hull(seed)
    if len(hull) < 3:
        raise ValueError("节点板轮廓种子不能形成多边形")
    outline = offset_convex_ccw(hull, 25.0)
    thickness = 8.0

    scene = trimesh.Scene()
    plate = extrude_polygon_mesh(outline, thickness)
    plate.visual.face_colors = [176, 182, 192, 255]
    plate_id = detail["gusset_component_id"]
    scene.add_geometry(plate, geom_name=str(plate_id))

    bolt_groups = []
    for group in groups:
        bolts = []
        # P6 sample contract fixes the modeled shank at 40 mm even if a noisy
        # source group carries a different parsed length.
        bolt_length = 40.0
        for x, y in group["holes"]:
            # Plate occupies z=[0,8]. Head sits immediately above its top face;
            # shank top is at z=8 and extends downward by 40 mm.
            head = trimesh.creation.cylinder(radius=13.9, height=10.0, sections=6)
            head.apply_translation((x, y, thickness + 5.0))
            shank = trimesh.creation.cylinder(radius=8.0, height=bolt_length, sections=12)
            shank.apply_translation((x, y, thickness - bolt_length / 2.0))
            bolts.extend((head, shank))
        merged = trimesh.util.concatenate(bolts)
        merged.visual.face_colors = [70, 76, 86, 255]
        scene.add_geometry(merged, geom_name=str(group["component_id"]))
        bolt_groups.append({
            "id": group["component_id"],
            "count": len(group["holes"]),
            "hole_dia": float(group["hole_diameter_mm"] or 17.5),
            "bolt_dia": float(group["diameter_mm"] or 16.0),
            "bolt_len": bolt_length,
        })

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = out_dir / "lod3_sample.glb"
    scene.export(str(glb_path), include_normals=True)
    report = {
        "detail_id": detail["detail_id"],
        "plate": {
            "outline_method": "convex_hull(polygon_local ∪ 全部孔心) 外扩 25mm",
            "n_holes": len(all_holes),
            "thickness_mm": thickness,
        },
        "bolt_groups": bolt_groups,
        "totals": {
            "n_groups": len(bolt_groups),
            "n_bolts": sum(group["count"] for group in bolt_groups),
        },
    }
    (out_dir / "lod3_sample.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if verify:
        verify_glb(glb_path)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="详图页 03 → LOD3 节点板及螺栓实体样板")
    parser.add_argument("--sheet", default=str(DEFAULT_SHEET))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = generate(Path(args.sheet), Path(args.out_dir), args.verify)
    except (OSError, ValueError, AssertionError, json.JSONDecodeError) as exc:
        print(f"LOD3 生成失败：{exc}", file=sys.stderr)
        return 2
    print(f"LOD3 完成：detail {report['detail_id']} | 组 {report['totals']['n_groups']} | "
          f"螺栓 {report['totals']['n_bolts']}")
    print(f"  → {Path(args.out_dir) / 'lod3_sample.glb'}")
    print(f"  → {Path(args.out_dir) / 'lod3_sample.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
