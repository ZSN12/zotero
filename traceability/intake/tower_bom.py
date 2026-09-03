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
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

_log = logging.getLogger(__name__)

from ..model import (
    Component,
    Dimension,
    DimensionOrigin,
    EngineeringModel,
    SourceRef,
    SourceType,
)


# P5 约束残差（2026-09-03）：BOM 行分类器。guowang 合并 BOM 实测 204 行
# 里 103 行是杆件（角钢），其余是垫板/螺栓/mangled 文本碎片，全部挤在
# 同一 bar_id 命名空间——非杆件行建 dim_bom_* 交叉核验是范畴错误
# （拿 5M16X40 螺栓行核 tower_bar 截面必然 FAILED，实测 r_bom_section_match
# 的 10 根「截面不符」全部源于此）。分类不丢数据：所有行照常进 bom_row
# 组件；只有 member 行才建交叉核验维度。
_BAR_ID_RE = re.compile(r"^\d{1,4}[A-Za-z]?$")
_STEEL_PREFIX_RE = re.compile(r"^(Q345|Q355|Q235|Q420|16MN)", re.IGNORECASE)


def classify_bom_row(bar_id: str, section: str) -> str:
    """BOM 行分类：member / plate / bolt / mangled。

    member —— 杆件行（角钢截面，可选钢种前缀）→ 参与杆件交叉核验；
    plate  —— 垫板/节点板行（'-6X40'、'Q345-14X260'）；
    bolt   —— 螺栓行（'5M16X40'）；
    mangled —— CAD 转义/列错位碎片（'\\\\M+5B9E6…'、bar_id 非件号形态）。
    """
    sid = (bar_id or "").strip()
    sec = (section or "").strip()
    if not sid or "\\M" in sid or "\\M" in sec or not _BAR_ID_RE.match(sid):
        return "mangled"
    sec_u = _STEEL_PREFIX_RE.sub("", sec).upper()
    if sec_u.startswith("L") and any(c.isdigit() for c in sec_u):
        return "member"
    if re.match(r"^\d+M\d+", sec_u) or sec_u.startswith("M"):
        return "bolt"
    return "plate"


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
        # P5 约束残差（2026-09-03）：行分类落盘（不丢数据），非 member 行
        # 不建交叉核验维度——螺栓/垫板行核 tower_bar 是范畴错误。
        row_class = classify_bom_row(bid, row.get("section", ""))
        row = dict(row)
        row["row_class"] = row_class
        model.add_component(Component(
            id=f"bom_{bid}",
            name=f"BOM 行 {bid}",
            kind="bom_row",
            source=SourceRef(SourceType.VENDOR, "tower_bom.csv", confidence=0.95),
            properties=row,
        ))

        if row_class != "member":
            continue

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
            except Exception as exc:
                # P4：块引用损坏时跳过该 INSERT 继续，但记录 debug 而非静默吞。
                _log.debug("INSERT 块展开失败，跳过：%s", exc)
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


def parse_bom_dxf_anchored(
    dxf_path: str | Path,
    layer_map_path: Optional[str | Path] = None,
    *,
    part_no_x_min: Optional[float] = None,
    part_no_range: Tuple[int, int] = (100, 999),
) -> List[Dict]:
    """C2：国网材料表「件号锚点」解析——不依赖中文表头。

    国网施工图的材料表表头是 SHX 形文件（ezdxf 读出为 \\M+XXXX 乱码），
    无法靠表头关键词映射列。但材料表结构固定：每一行以「件号」（纯数字）
    开头，其后按列序为 截面 / 长度(mm) / 数量 / 单重 / 总重 / 备注。

    本解析器以件号为锚点：
        * 找出所有「纯数字」文本，落在 part_no_range 内且 x 集中（同列）的
          视为件号列；
        * 每个件号所在行，按 x 升序取其后文本，依次识别：
          - section：匹配截面正则（L\\d+X\\d+ / Q345L\\d+X\\d+ / Q345-\\d+ 等）
          - length_mm：首个纯数字
          - qty：下一个纯数字
        * 无表头、无中文、编码损坏都成立；读不到就留空/0，绝不编造。

    返回与 parse_bom_csv 相同结构 list[dict]（bar_id/section/length_mm/qty/name）。
    """
    import re

    import ezdxf

    dxf_path = str(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    section_re = re.compile(
        r"^(?:Q345)?L\d{1,3}\s*[Xx×*]\d{1,3}$"   # L40X3 / Q345L70X5
        r"|^Q345-?\d+[Xx×*]\d+$"                  # Q345-6X207
        r"|^-?\d+[Xx×*]\d+$"                      # -6X207 (钢板厚x宽)
        r"|^Q345-?\d*$"                           # Q345-14
        r"|^Q\d+$"                                # Q235
    )
    part_no_re = re.compile(r"^\d{1,5}$")

    texts: List[Dict] = []
    for e in msp:
        if e.dxftype() == "INSERT":
            try:
                for v in e.virtual_entities():
                    if v.dxftype() in ("TEXT", "MTEXT"):
                        texts.append(_text_record(v))
            except Exception as exc:
                # P4：块引用损坏时跳过该 INSERT 继续，但记录 debug 而非静默吞。
                _log.debug("INSERT 块展开失败，跳过：%s", exc)
        elif e.dxftype() in ("TEXT", "MTEXT"):
            texts.append(_text_record(e))

    if not texts:
        return []

    # 1) 找件号列：纯数字、落在 part_no_range、x 集中（众数 x 附近）
    part_no_candidates = [
        t for t in texts
        if part_no_re.fullmatch(t["text"])
        and part_no_range[0] <= int(t["text"]) <= part_no_range[1]
    ]
    if not part_no_candidates:
        return []

    # 件号列定位：材料表件号列是「出现次数最多」的紧密数字列。
    # 在 part_no_range=(100,999) 约束下，件号列 x=34952 出现 59 次，
    # 长度列 x=34992（如 836/754）只 34 次、几何图散点 <6 次，
    # 因此众数天然锁定件号列。
    from collections import Counter
    x_counter = Counter(round(t["x"]) for t in part_no_candidates)
    part_no_x = float(x_counter.most_common(1)[0][0])
    if part_no_x_min is not None and part_no_x < part_no_x_min:
        return []

    # 件号列容差：取件号 x 的紧密簇
    x_tol = 3.0
    part_nos = [
        t for t in part_no_candidates
        if abs(t["x"] - part_no_x) <= x_tol
    ]
    if not part_nos:
        return []

    # 2) 按 y 聚类成行，每行内按 x 排序
    rows = _cluster_table_rows(texts)
    part_no_by_y = {round(t["y"] / 3): t for t in part_nos}

    out: List[Dict] = []
    for row in rows:
        if not row:
            continue
        cells = sorted(row, key=lambda d: d["x"])
        # 该行的件号锚点：行内第一个纯数字且 x≈part_no_x
        anchor_idx = None
        anchor_val = None
        for i, c in enumerate(cells):
            if part_no_re.fullmatch(c["text"]) and abs(c["x"] - part_no_x) <= x_tol:
                anchor_idx = i
                anchor_val = c["text"].strip()
                break
        if anchor_idx is None:
            continue

        bar_id = anchor_val
        # 3) 件号之后按列序解析：截面 / 长度 / 数量
        tail = cells[anchor_idx + 1:]
        section = ""
        length_mm = 0.0
        qty = 1
        seen_length = False
        for c in tail:
            t = c["text"].strip()
            if not t:
                continue
            if not section and section_re.match(t):
                section = t
                continue
            if part_no_re.fullmatch(t):
                num = float(t)
                if not seen_length:
                    length_mm = num
                    seen_length = True
                else:
                    qty = int(num)
                    break  # 长度、数量都拿到即停
        if section or length_mm > 0:
            out.append({
                "bar_id": bar_id,
                "section": section,
                "length_mm": length_mm,
                "qty": qty,
                "name": "",
            })
    return out


def parse_bom_auto(path: str | Path, layer_map_path: Optional[str | Path] = None) -> List[Dict]:
    """按扩展名自动选择 BOM 解析器（CSV 或 DXF）。"""
    path = Path(path)
    if path.suffix.lower() == ".dxf":
        # 优先用件号锚点解析（对国网 SHX 乱码表头更稳健）；空则回退旧表头法
        anchored = parse_bom_dxf_anchored(path, layer_map_path=layer_map_path)
        if anchored:
            return anchored
        return parse_bom_dxf(path, layer_map_path=layer_map_path)
    return parse_bom_csv(path)
