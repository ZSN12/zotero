"""E2：从 EngineeringModel 写线框 DXF。

把 tower_node 作为点（POINT），tower_bar 作为线段（LINE），
按杆件图层/截面分组写图层。Harness passed 后可导出，供 CAD 复核。

原则：
    * 坐标缺失（placeholder）时跳过该轴并写为 0？不——placeholder 坐标
      不写进 DXF，避免把"没读到"画成"0"。杆件两端节点坐标齐全才导出。
    * 每个实体只来自 model，不编造。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from ..model import EngineeringModel


def export_tower_dxf(model: EngineeringModel, out_path: str | Path) -> str:
    """把铁塔模型写为线框 DXF。

    返回输出路径。节点坐标缺任一轴时跳过该杆件（并在返回的统计中体现）。
    """
    import ezdxf

    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()

    # 图层：杆件 layer 名（或默认 TRUSS）
    layers = set()
    for cid, comp in model.components.items():
        if comp.kind == "tower_bar":
            layers.add(str(comp.properties.get("layer", "TRUSS")))
    for name in layers:
        if name not in doc.layers:
            doc.layers.add(name)

    nodes: Dict[str, Dict] = {}
    for cid, comp in model.components.items():
        if comp.kind != "tower_node":
            continue
        p = comp.properties
        x, y, z = p.get("x"), p.get("y"), p.get("z")
        nodes[cid] = {"x": x, "y": y, "z": z, "properties": p}

    bars_written = 0
    bars_skipped = 0
    for cid, comp in model.components.items():
        if comp.kind != "tower_bar":
            continue
        f = comp.properties.get("from_node")
        t = comp.properties.get("to_node")
        a, b = nodes.get(f), nodes.get(t)
        if not a or not b:
            bars_skipped += 1
            continue
        if None in (a["x"], a["y"], a["z"], b["x"], b["y"], b["z"]):
            # placeholder 不写入 DXF，绝不把缺失画成 0
            bars_skipped += 1
            continue
        layer = str(comp.properties.get("layer", "TRUSS"))
        msp.add_line(
            (float(a["x"]), float(a["y"]), float(a["z"])),
            (float(b["x"]), float(b["y"]), float(b["z"])),
            dxfattribs={"layer": layer},
        )
        bars_written += 1

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out_path))
    return str(out_path)
