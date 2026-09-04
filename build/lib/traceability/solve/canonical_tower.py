"""L0 CanonicalTower —— 权威几何（唯一 3D 真值源）。

设计原则（见重构计划 L0）：
    * 一种几何只从一个源来：完整铁塔 3D 只来自国网 GIM `.mod` / 计算 `.NODE`，
      不再从施工图（DXF）去「发明」3D。
    * 导 GLB / 线框 OBJ 只走本层，禁止 synthetic_side、四面展开、门禁放宽。
    * 固定 schema：{nodes, bars, units, up}。

schema（固定）：
    nodes: {node_id: [x, y, z]}            # mm，Z 为高度轴（up="Z"）
    bars:  [{id, from, to, section, material}]   # from/to 引用 nodes 的键
    units: "mm"
    up:    "Z"

数据来源：
    * GIM `.mod`：节点 P 行 + 杆段 R 行（R 保留为杆段图，不预先合并成长杆；
      合并是可选后处理）。
    * 计算文件 `.NODE`：标准呼高独立塔的节点清单（可选，用于提纯单塔）。
    * `.MF`：杆件截面/材料交叉校验（节点号应对上）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..model import Component, EngineeringModel, SourceRef, SourceType

# 权威默认源：国网官方资料包里的 GIM .mod 与计算文件 .NODE
DEFAULT_MOD = (
    Path.home() / "Downloads"
    / "输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)"
    / "GIM/35A1/35A1-JC1/35A1-JC1-GIM输出/解析成果/35A1-JC1.mod"
)
DEFAULT_NODE = (
    Path.home() / "Downloads"
    / "输电线路铁塔国网2019版35kV输电线路典型设计(计算+CAD+模型)"
    / "计算文件/35A/35A1/35A1-JC1/35A1-JC1.NODE"
)

# 仓库内已提纯的 GT（标准 30m 呼高单塔，来自 计算 .NODE + GIM .mod）
REPO_GT = Path(__file__).resolve().parent.parent.parent / "examples/gt/35A1-JC1_ground_truth.json"


class CanonicalTower:
    """不可变的规范铁塔：nodes + bars + units + up。"""

    __slots__ = ("nodes", "bars", "units", "up", "name", "source")

    def __init__(
        self,
        nodes: Dict[str, Sequence[float]],
        bars: List[dict],
        *,
        units: str = "mm",
        up: str = "Z",
        name: str = "CanonicalTower",
        source: str = "",
    ) -> None:
        self.nodes = dict(nodes)
        self.bars = list(bars)
        self.units = units
        self.up = up
        self.name = name
        self.source = source

    # -- 序列化 --------------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "units": self.units,
            "up": self.up,
            "nodes": self.nodes,
            "bars": self.bars,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CanonicalTower":
        return cls(
            d.get("nodes", {}),
            d.get("bars", []),
            units=d.get("units", "mm"),
            up=d.get("up", "Z"),
            name=d.get("name", "CanonicalTower"),
            source=d.get("source", ""),
        )

    # -- 统计 ----------------------------------------------------------- #
    def bar_count(self) -> int:
        return len(self.bars)

    def node_count(self) -> int:
        return len(self.nodes)

    def bbox(self) -> Dict[str, Tuple[float, float]]:
        xs = [v[0] for v in self.nodes.values()]
        ys = [v[1] for v in self.nodes.values()]
        zs = [v[2] for v in self.nodes.values()]
        return {
            "x": (min(xs), max(xs)) if xs else (0.0, 0.0),
            "y": (min(ys), max(ys)) if ys else (0.0, 0.0),
            "z": (min(zs), max(zs)) if zs else (0.0, 0.0),
        }

    def to_engineering_model(self, prefix: str = "ct_") -> EngineeringModel:
        """转成 traceability 的 EngineeringModel，供现有 GLB 导出复用。"""
        model = EngineeringModel(name=self.name)
        for nid, xyz in self.nodes.items():
            model.components[f"{prefix}node_{nid}"] = Component(
                id=f"{prefix}node_{nid}",
                kind="tower_node",
                name=f"CT node {nid}",
                properties={
                    "node_id": str(nid),
                    "x": float(xyz[0]),
                    "y": float(xyz[1]),
                    "z": float(xyz[2]),
                    "solve_status": "solved",
                },
                source=SourceRef(source_type=SourceType.DERIVED, reference="canonical_tower"),
            )
        for i, bar in enumerate(self.bars):
            bid = bar["id"]
            fn, tn = f"{prefix}node_{bar['from']}", f"{prefix}node_{bar['to']}"
            model.components[f"{prefix}bar_{bid}"] = Component(
                id=f"{prefix}bar_{bid}",
                kind="tower_bar",
                name=f"CT {bid}",
                properties={
                    "bar_id": bid,
                    "from_node": fn,
                    "to_node": tn,
                    "section": bar.get("section"),
                    "material": bar.get("material"),
                },
                source=SourceRef(source_type=SourceType.DERIVED, reference="canonical_tower"),
            )
        return model


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def parse_mod(path: Path) -> Tuple[Dict[str, Sequence[float]], List[dict]]:
    """解析 GIM .mod 为 (nodes, bars)。

    bars 保留为杆段图（每条 R 一行），不预先合并成长杆；
    合并是调用方的可选后处理（见 merge_segments）。
    """
    nodes: Dict[str, Sequence[float]] = {}
    bars: List[dict] = []
    seg_idx = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("P,"):
            p = s.split(",")
            nodes[p[1]] = [float(p[2]), float(p[3]), float(p[4])]
        elif s.startswith("R,"):
            p = s.split(",")
            seg_idx += 1
            bars.append({
                "id": f"PM_{seg_idx:04d}",
                "from": p[1],
                "to": p[2],
                "section": p[3],
                "material": p[4],
            })
    return nodes, bars


def parse_node_file(path: Path) -> set[str]:
    """解析计算文件 .NODE，返回该座独立铁塔的节点 ID 集合（字符串键）。"""
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                int(parts[0])
                ids.add(parts[0])
            except ValueError:
                continue
    return ids


def merge_segments(bars: List[dict]) -> List[dict]:
    """把「端点相接 + 截面/材质相同」的杆段合并回物理杆件（可选后处理）。

    保留分段图（不合并）时可直接用 bars；需要完整杆件时再调用本函数。
    from/to 是合并后链的首尾节点 id；segments 是参与的原始段数。
    """
    from collections import defaultdict

    by_from = defaultdict(list)
    for i, b in enumerate(bars):
        by_from[b["from"]].append(i)
    used: set[int] = set()
    out: List[dict] = []
    for i, b in enumerate(bars):
        if i in used:
            continue
        chain = [b]
        used.add(i)
        cur = b
        while True:
            nxt = None
            for j, cand in enumerate(bars):
                if j in used:
                    continue
                if (cand["from"] == cur["to"] and cand["section"] == b["section"]
                        and cand["material"] == b["material"]):
                    nxt = j
                    break
            if nxt is None:
                break
            cur = bars[nxt]
            used.add(nxt)
            chain.append(cur)
        out.append({
            "id": b["id"],
            "from": b["from"],
            "to": cur["to"],
            "section": b["section"],
            "material": b["material"],
            "segments": len(chain),
        })
    return out


def load_from_mod(
    mod_path: Path,
    *,
    node_file: Optional[Path] = None,
    merge: bool = False,
    name: str = "CanonicalTower",
) -> CanonicalTower:
    """从 GIM .mod 构建 CanonicalTower。

    node_file 给定则提纯为标准呼高单塔（剔除其余呼高的冲突杆件）。
    merge=True 时把杆段合并回物理杆件；否则保留杆段图。
    """
    nodes, bars = parse_mod(mod_path)
    if node_file and node_file.exists():
        keep = parse_node_file(node_file)
        nodes = {k: v for k, v in nodes.items() if k in keep}
        bars = [b for b in bars if b["from"] in keep and b["to"] in keep]
    if merge:
        bars = merge_segments(bars)
    src = str(mod_path)
    if node_file:
        src += f" ; node={node_file}"
    return CanonicalTower(nodes, bars, units="mm", up="Z", name=name, source=src)


def load_gt(path: Optional[Path] = None) -> CanonicalTower:
    """从仓库内已提纯的 GT JSON（或任意 CanonicalTower 格式 JSON）加载。"""
    p = path or REPO_GT
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    return CanonicalTower.from_dict(d)


def save(tower: CanonicalTower, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(tower.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# 导出（只走本层，禁止 DXF 合成 / 四面展开 / 门禁放宽）
# --------------------------------------------------------------------------- #
def export_glb(tower: CanonicalTower, out_path: Path, *, strict: bool = True) -> Path:
    """从 CanonicalTower 导出 GLB（只走正确渲染路径，不跑 DXF 合成）。

    依赖 traceability.solve.tower_solver.export_tower_glb（含 Phase 0 修正后的
    杆件实体化：杆轴 = 局部 Z -> 世界方向，端点落节点）。
    """
    from .tower_solver import export_tower_glb

    model = tower.to_engineering_model()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    export_tower_glb(model, out_path, strict=strict)
    return out_path


def export_wireframe_obj(tower: CanonicalTower, out_path: Path) -> Path:
    """导出线框 OBJ（仅节点-杆件中心线，便于对比 bbox / 投影）。"""
    import numpy as np

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    for nid, xyz in tower.nodes.items():
        lines.append(f"v {xyz[0]:.3f} {xyz[1]:.3f} {xyz[2]:.3f}")
    node_idx = {nid: i + 1 for i, nid in enumerate(tower.nodes)}
    for b in tower.bars:
        if b["from"] in node_idx and b["to"] in node_idx:
            lines.append(f"l {node_idx[b['from']]} {node_idx[b['to']]}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
