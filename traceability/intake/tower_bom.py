"""铁塔 BOM 解析与交叉核验（Phase 2）。

从 CSV/Excel 或 DXF 内表格解析 BOM 行（bar_id, section, length_mm, qty），
与 tower_bar 按 bar_id 匹配，产出交叉核验维度与规则。

原则：
    * BOM 是独立来源，不覆盖图纸读数，而是并列为 cross-check 维度
    * 偏差超阈值 → 规则 failed，不悄悄改值
    * 同一 bar_id 可能对应多个投影视图（bar_G01_elevation / bar_G01_plan），
      维度按 bar_id 聚合，applies_to 指向实际存在的构件 ID。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Optional

from ..model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
)


def parse_bom_csv(csv_path: str | Path) -> List[Dict]:
    """解析 BOM CSV：bar_id, section, length_mm, qty。"""
    rows: List[Dict] = []
    with Path(csv_path).open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "bar_id": row["bar_id"].strip(),
                "section": row.get("section", "").strip(),
                "length_mm": float(row.get("length_mm", 0) or 0),
                "qty": int(row.get("qty", 1) or 1),
            })
    return rows


def _bars_by_bar_id(model: EngineeringModel) -> Dict[str, List[str]]:
    """bar_id -> 属于它的 tower_bar 构件 ID 列表（跨视图投影）。"""
    by_id: Dict[str, List[str]] = {}
    for cid, comp in model.components.items():
        if comp.kind != "tower_bar":
            continue
        bid = comp.properties.get("bar_id")
        if not bid or bid.startswith("UNLABELED"):
            continue
        by_id.setdefault(bid, []).append(cid)
    return by_id


def cross_check_bom(model: EngineeringModel, bom_rows: List[Dict]) -> EngineeringModel:
    """把 BOM 行并入模型并建立交叉核验。

    BOM 行 -> bom_row Component；
    每个 bar_id 生成两条 Dimension：
        * dim_bom_length_{bar_id}  (measured, 来自 BOM)
        * dim_bom_section_{bar_id} (measured, 来自 BOM)
    applies_to 指向实际存在的 tower_bar 构件 ID（按 bar_id 聚合匹配），
    并为每根匹配到的 tower_bar 建立依赖。
    """
    bars_by_id = _bars_by_bar_id(model)

    for row in bom_rows:
        bid = row["bar_id"]
        model.add_component(Component(
            id=f"bom_{bid}",
            name=f"BOM 行 {bid}",
            kind="bom_row",
            source=SourceRef(SourceType.VENDOR, "tower_bom.csv", confidence=0.95),
            properties=row,
        ))

        matched = bars_by_id.get(bid, [])
        # 优先挂到立面/主视图投影；没有匹配则挂到 BOM 行自身，保证引用不悬空
        applies_to = matched[0] if matched else f"bom_{bid}"

        dim_len = Dimension(
            id=f"dim_bom_length_{bid}",
            name=f"{bid} BOM 长度",
            value=row["length_mm"],
            unit="mm",
            origin=DimensionOrigin.MEASURED,
            source=SourceRef(SourceType.VENDOR, "tower_bom.csv", confidence=0.95),
            applies_to=applies_to,
        )
        dim_sec = Dimension(
            id=f"dim_bom_section_{bid}",
            name=f"{bid} BOM 截面",
            value=row["section"],
            unit="",
            origin=DimensionOrigin.MEASURED,
            source=SourceRef(SourceType.VENDOR, "tower_bom.csv", confidence=0.95),
            applies_to=applies_to,
        )
        model.add_dimension(dim_len)
        model.add_dimension(dim_sec)

        # 每根匹配投影都依赖这两条 BOM 维度
        for cid in matched:
            model.depend(cid, dim_len.id, dim_sec.id)

    return model


def parse_bom_dxf(
    dxf_path: str | Path,
    layer_map_path: Optional[str | Path] = None,
) -> List[Dict]:
    """C1：解析 *-ML.dwg 转 DXF 后的表格 → BOM 行。

    表格结构识别：
        * TEXT/MTEXT（含块内文本）按 y 聚类成行、按 x 排序成列；
        * 有表头（图号/件号/截面/长度/数量/名称/备注）时按表头映射列；
        * 无表头时按常见顺序回退：bar_id / section / length_mm / qty。

    返回与 parse_bom_csv 相同结构的 list[dict]；
    读不到截面/长度时给空字符串/0，绝不编造数值。
    """
    import ezdxf

    dxf_path = str(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    texts: List[Dict] = []
    for e in msp:
        if e.dxftype() == "INSERT":
            try:
                for v in e.virtual_entities():
                    if v.dxftype() in ("TEXT", "MTEXT"):
                        texts.append(_text_record(v))
            except Exception:
                pass
        elif e.dxftype() in ("TEXT", "MTEXT"):
            texts.append(_text_record(e))

    rows = _cluster_table_rows(texts)
    if not rows:
        return []

    # 找表头行：优先选含最多 BOM 关键字、且列数 >= 2 的行
    header_keywords = ("图号", "件号", "编号", "截面", "规格", "长度", "数量", "张数",
                       "图名", "名称", "序号", "备注")
    best_idx, best_score = None, 0
    for i, row in enumerate(rows):
        if len(row) < 2:
            continue
        joined = "".join(c["text"] for c in row).replace(" ", "").replace("\t", "")
        score = sum(1 for k in header_keywords if k in joined)
        if score > best_score:
            best_score, best_idx = score, i

    if best_idx is not None and best_score > 0:
        mapping = _map_header_columns(rows[best_idx])
        header_y = rows[best_idx][0]["y"]
        # 只取表头下方的行（图纸表格通常从上往下排列），过滤标题栏
        data_rows = [row for row in rows[best_idx + 1:] if row[0]["y"] < header_y]
    else:
        mapping = {"bar_id": 0, "section": 1, "length_mm": 2, "qty": 3, "name": 2}
        data_rows = [row for row in rows if len(row) >= 2]

    out: List[Dict] = []
    for row in data_rows:
        cells = {k: (row[idx]["text"] if idx < len(row) else "") for k, idx in mapping.items()}
        bar_id = cells.get("bar_id", "").strip()
        if not bar_id or bar_id in ("序号", "图号", "件号"):
            continue
        sec = cells.get("section", "").strip()
        try:
            length = float(cells.get("length_mm", 0) or 0)
        except (TypeError, ValueError):
            length = 0.0
        try:
            qty = int(float(cells.get("qty", 1) or 1))
        except (TypeError, ValueError):
            qty = 1
        out.append({
            "bar_id": bar_id,
            "section": sec,
            "length_mm": length,
            "qty": qty,
            "name": cells.get("name", "").strip(),
        })
    return out


def _text_record(e) -> Dict:
    text = e.dxf.text if e.dxftype() == "TEXT" else getattr(e, "text", "")
    return {
        "text": (text or "").strip(),
        "x": float(e.dxf.insert.x),
        "y": float(e.dxf.insert.y),
    }


def _cluster_table_rows(texts: List[Dict], y_tol: Optional[float] = None) -> List[List[Dict]]:
    """按 y 聚类成行，每行内按 x 排序。"""
    if not texts:
        return []
    ys = sorted(t["y"] for t in texts)
    if y_tol is None:
        # 用相邻 y 间隔中位数的一半估计行距容差
        gaps = sorted(b - a for a, b in zip(ys, ys[1:]) if b - a > 0.01)
        y_tol = max(gaps[len(gaps) // 2] * 0.4, 1.0) if gaps else 3.0
        y_tol = min(y_tol, 10.0)

    rows: List[List[Dict]] = []
    for t in sorted(texts, key=lambda d: (-d["y"], d["x"])):
        placed = False
        for row in rows:
            if abs(row[0]["y"] - t["y"]) <= y_tol:
                row.append(t)
                placed = True
                break
        if not placed:
            rows.append([t])
    return [sorted(row, key=lambda d: d["x"]) for row in rows]


def _map_header_columns(header: List[Dict]) -> Dict[str, int]:
    """把表头列文本映射到 BOM 字段。"""
    mapping: Dict[str, int] = {}
    for idx, cell in enumerate(header):
        txt = cell["text"].strip().replace(" ", "").replace("\t", "")
        if "图号" in txt or "编号" in txt or "件号" in txt:
            mapping.setdefault("bar_id", idx)
        elif "截面" in txt or "规格" in txt:
            mapping.setdefault("section", idx)
        elif "长度" in txt:
            mapping.setdefault("length_mm", idx)
        elif "数量" in txt or "张数" in txt:
            mapping.setdefault("qty", idx)
        elif "名称" in txt or "图名" in txt:
            mapping.setdefault("name", idx)
    # 缺省回退：bar_id 至少映射到第一列
    mapping.setdefault("bar_id", 0)
    return mapping


def parse_bom_auto(path: str | Path, layer_map_path: Optional[str | Path] = None) -> List[Dict]:
    """按扩展名自动选择 BOM 解析器（CSV 或 DXF）。"""
    path = Path(path)
    if path.suffix.lower() == ".dxf":
        return parse_bom_dxf(path, layer_map_path=layer_map_path)
    return parse_bom_csv(path)
