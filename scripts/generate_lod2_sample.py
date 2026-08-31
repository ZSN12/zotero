#!/usr/bin/env python3
"""Generate a LOD2 sample panel whose tower bars use solid L-angle sections."""
from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = REPO_ROOT / "out/35A1-JC1-full-deliver/model.json"
FALLBACK_MODEL = REPO_ROOT / "web/demo/35A1-JC1/latest_deliver/model.json"
DEFAULT_OUT_DIR = REPO_ROOT / "out/35A1-JC1-lod-samples"
SOURCE_FILE = "35A1-JC1-06"


def load_model(path: Path, fallback: Path = FALLBACK_MODEL) -> Tuple[dict, Path]:
    """Read a possibly-rewritten model, retrying transient JSON truncation."""
    path = Path(path)
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8")), path
        except json.JSONDecodeError:
            if attempt < 4:
                time.sleep(2)
    if fallback == path:
        raise ValueError(f"模型连续 5 次无法解析：{path}")
    return json.loads(Path(fallback).read_text(encoding="utf-8")), Path(fallback)


def parse_section(section: object) -> Tuple[str, float, float]:
    """Return normalized section text, leg and thickness; unknown means L50X4."""
    text = str(section or "").strip().upper().replace("×", "X")
    text = re.sub(r"^Q345\s*", "", text)
    match = re.search(r"L\s*(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)", text)
    if not match:
        return "L50X4", 50.0, 4.0
    leg, thickness = float(match.group(1)), float(match.group(2))
    if leg <= 0 or thickness <= 0 or thickness >= leg:
        return "L50X4", 50.0, 4.0
    return f"L{match.group(1)}X{match.group(2)}", leg, thickness


def _node_positions(model: dict) -> Dict[str, Tuple[float, float, float]]:
    positions = {}
    for cid, component in model.get("components", {}).items():
        if component.get("kind") != "tower_node":
            continue
        props = component.get("properties", {})
        try:
            positions[cid] = tuple(float(props.get(axis) or 0.0) for axis in "xyz")
        except (TypeError, ValueError):
            continue
    return positions


def select_panel_bars(model: dict, z_lo: float, z_hi: float) -> Tuple[Dict[str, dict], List[float]]:
    """Select source-06 bars inside the requested range, or its nearest occupied band."""
    nodes = _node_positions(model)
    candidates: Dict[str, dict] = {}
    for cid, component in model.get("components", {}).items():
        if component.get("kind") != "tower_bar":
            continue
        props = component.get("properties", {})
        if props.get("source_file") != SOURCE_FILE:
            continue
        a, b = nodes.get(props.get("from_node")), nodes.get(props.get("to_node"))
        if a is None or b is None or math.dist(a, b) < 1e-9:
            continue
        candidates[cid] = {"a": a, "b": b, "properties": props}
    if not candidates:
        raise ValueError(f"模型中没有 {SOURCE_FILE} 的可实体化 tower_bar")

    selected = {
        cid: bar for cid, bar in candidates.items()
        if all(z_lo <= point[2] <= z_hi for point in (bar["a"], bar["b"]))
    }
    if selected:
        return selected, [float(z_lo), float(z_hi)]

    # No bar lies fully in the request. Choose the closest occupied elevation span,
    # then include every bar fully contained by that actual span.
    target = (z_lo + z_hi) / 2.0
    nearest = min(
        candidates.values(),
        key=lambda bar: abs(((bar["a"][2] + bar["b"][2]) / 2.0) - target),
    )
    actual_lo = min(nearest["a"][2], nearest["b"][2])
    actual_hi = max(nearest["a"][2], nearest["b"][2])
    selected = {
        cid: bar for cid, bar in candidates.items()
        if actual_lo <= bar["a"][2] <= actual_hi and actual_lo <= bar["b"][2] <= actual_hi
    }
    if not selected:
        raise ValueError("无法找到邻近的非空节间")
    return selected, [float(actual_lo), float(actual_hi)]


def _bar_transform(pa, pb):
    import numpy as np
    import trimesh.transformations as tf

    direction = np.asarray(pb, dtype=float) - np.asarray(pa, dtype=float)
    length = float(np.linalg.norm(direction))
    if length < 1e-9:
        return None, 0.0
    direction /= length
    z_axis = np.array([0.0, 0.0, 1.0])
    axis = np.cross(z_axis, direction)
    sine = float(np.linalg.norm(axis))
    cosine = float(np.clip(np.dot(z_axis, direction), -1.0, 1.0))
    matrix = np.eye(4)
    if sine < 1e-9:
        if cosine < 0:
            matrix = np.diag([1.0, -1.0, -1.0, 1.0])
    else:
        matrix = tf.rotation_matrix(math.acos(cosine), axis / sine)
    matrix = np.asarray(matrix, dtype=float)
    matrix[:3, 3] = (np.asarray(pa, dtype=float) + np.asarray(pb, dtype=float)) / 2.0
    return matrix, length


def l_angle_mesh(leg: float, thickness: float, length: float):
    """Create a watertight six-vertex L polygon extruded about local Z."""
    import numpy as np
    import trimesh

    polygon = [(0.0, 0.0), (leg, 0.0), (leg, thickness),
               (thickness, thickness), (thickness, leg), (0.0, leg)]
    vertices = np.array([(x, y, z) for z in (-length / 2.0, length / 2.0)
                         for x, y in polygon], dtype=float)
    top_triangles = [(0, 1, 2), (0, 2, 3), (0, 3, 5), (3, 4, 5)]
    faces = []
    for a, b, c in top_triangles:
        faces.append((c, b, a))
        faces.append((a + 6, b + 6, c + 6))
    for i in range(6):
        j = (i + 1) % 6
        faces.extend(((i, j, j + 6), (i, j + 6, i + 6)))
    return trimesh.Trimesh(vertices=vertices, faces=np.asarray(faces), process=True)


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


def generate(model_path: Path, out_dir: Path, z_lo: float = 14000.0,
             z_hi: float = 16000.0, verify: bool = False) -> dict:
    import trimesh

    model, loaded_from = load_model(Path(model_path))
    bars, panel_z = select_panel_bars(model, z_lo, z_hi)
    scene = trimesh.Scene()
    report_bars = {}
    for cid, bar in sorted(bars.items()):
        matrix, length = _bar_transform(bar["a"], bar["b"])
        if matrix is None:
            continue
        section, leg, thickness = parse_section(bar["properties"].get("section"))
        mesh = l_angle_mesh(leg, thickness, length)
        mesh.apply_transform(matrix)
        mesh.visual.face_colors = [125, 145, 170, 255]
        scene.add_geometry(mesh, geom_name=str(cid))
        theoretical = (2.0 * leg * thickness - thickness ** 2) * length
        volume = abs(float(mesh.volume))
        error = abs(volume - theoretical) / theoretical
        assert error < 0.02, f"{cid} 体积误差 {error:.3%} 超过 2%"
        report_bars[cid] = {
            "section": section,
            "length_mm": length,
            "volume_mm3": volume,
            "theoretical_mm3": theoretical,
            "volume_error": error,
            "centroid": [float(v) for v in mesh.center_mass],
        }
    if not report_bars:
        raise ValueError("所选节间没有可实体化杆件，不输出空产物")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = out_dir / "lod2_sample.glb"
    scene.export(str(glb_path), include_normals=True)
    max_error = max(item["volume_error"] for item in report_bars.values())
    report = {
        "bars": report_bars,
        "panel_z": panel_z,
        "summary": {
            "n_bars": len(report_bars),
            "max_volume_error": max_error,
            "source_file": SOURCE_FILE,
            "model_loaded_from": str(loaded_from),
        },
    }
    (out_dir / "lod2_sample.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if verify:
        verify_glb(glb_path)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EngineeringModel 节间 → LOD2 L 型角钢样板")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--z-lo", type=float, default=14000.0)
    parser.add_argument("--z-hi", type=float, default=16000.0)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if args.z_lo > args.z_hi:
        parser.error("--z-lo 不能大于 --z-hi")
    try:
        report = generate(Path(args.model), Path(args.out_dir), args.z_lo, args.z_hi, args.verify)
    except (OSError, ValueError, AssertionError, json.JSONDecodeError) as exc:
        print(f"LOD2 生成失败：{exc}", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"LOD2 完成：节间 z={report['panel_z']} | 杆 {summary['n_bars']} | "
          f"最大体积误差 {summary['max_volume_error']:.6%}")
    print(f"  → {Path(args.out_dir) / 'lod2_sample.glb'}")
    print(f"  → {Path(args.out_dir) / 'lod2_sample.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
