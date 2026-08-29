"""独立的比例尺自动标定模块（DIMENSION 实体 → 真实 scale_x / scale_y）。

背景
----
国网铁塔 DXF 立面图（35A1-JC1-02 等）中，overlay 硬编码的
``scale_x=20, scale_y=20`` 是错误的：真实比例逐 sheet 不同（10/20/100 混用），
且同一张图里主视图（塔身整体大跨距）与局部节点板大样的比例也混在一起。

本模块从图纸自带的 DIMENSION 尺寸标注实体自动标定真实比例。**严禁使用 GT
数据反推比例**（红线）——所有信息只来自 DIMENSION 实体自身的 ``text`` 字段与
``defpoint2`` / ``defpoint3`` 定义点距离。

算法要点
--------
对每个 region：

1. 收集 ``midpoint`` 落在 ``region['region'] = [x0, x1, y0, y1]`` 内的样本；
2. 按测量方向把样本拆成横向（|dx| > |dy|）与竖向（|dy| >= |dx|）；
3. 每个方向内部，样本比例 ``scale = text_value / measured_distance``；
4. 主视图判定：优先选「text_value 最大的一批样本」所在 cluster 的中位数，
   从而把节点板局部大样（文字值小、scale 小）与主视图（文字值大、scale 大）
   区分开；
5. 判定出有效 scale 后写回 ``region['scale_x']`` / ``region['scale_y']``，
   并记录 ``_scale_x_calibrated`` / ``_scale_y_calibrated`` /
   ``_scale_origin='dimension_calibration'`` 元数据。

本模块无任何 GT 依赖，只依赖 ezdxf modelspace 与纯数值计算。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

#: 常见图纸比例档位（含中间档 2.5，用于四舍五入归一）。
_COMMON_SCALES: Tuple[float, ...] = (1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 200.0)

#: 聚类容差（相对比例），用于把同一档位内的样本归为一簇。
_CLUSTER_TOLERANCE: float = 0.15

#: 主视图样本筛选阈值：text_value >= max_text * 该比例，即取最大一批大尺寸。
_MAX_TEXT_RATIO: float = 0.5

#: 主视图样本数量上限（取 text_value 最大的前 N 个）。
_TOP_N: int = 3

#: 距离下限，低于该值视为退化样本（defpoint 重合）。
_MIN_DISTANCE: float = 1e-4


@dataclass(frozen=True)
class DimSample:
    """一个 DIMENSION 实体的标定样本。"""

    text_value: float          # 真实 mm 值（如 5800.0）
    measured_distance: float   # 图纸单位测量距离（> 0）
    dx: float                  # defpoint3.x - defpoint2.x
    dy: float                  # defpoint3.y - defpoint2.y
    midpoint: Tuple[float, float]  # (defpoint2 + defpoint3) / 2


# ---------------------------------------------------------------------------
# 提取
# ---------------------------------------------------------------------------

_NUMERIC_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def _parse_text(text: Any) -> Optional[float]:
    """把 DIMENSION 的 text 字段解析为浮点 mm 值，失败返回 None。

    国网图 text 可能是 "5800"、"1 900"（含空格）、"1900mm"、"1,212"
    （千分位）等。宽松提取第一个可解析的数值 token。
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    # 优先直接按 float 解析（覆盖 "5800", "1212.5"）
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    # 去掉千分位逗号 / 空格后再试（"1,212" → "1212"、"1 900" → "1900"）
    for ch in (",", " "):
        cleaned = s.replace(ch, "")
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            continue
    # 兜底：正则提取首个数值 token（"1900mm" → 1900.0）
    m = _NUMERIC_RE.search(s)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None


def _point2(p: Any) -> Optional[Tuple[float, float]]:
    """取 DIMENSION 定义点的 (x, y)，缺失/异常返回 None。"""
    if p is None:
        return None
    try:
        x = float(p[0])
        y = float(p[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (x, y)


def extract_dim_samples(msp) -> List[DimSample]:
    """从 ezdxf modelspace 提取 DIMENSION 实体标定样本。

    - 遍历 ``msp.query('DIMENSION')``；
    - 提取 text（空或非数字则尝试解析，解析失败跳过）；
    - 读取 defpoint2 / defpoint3，计算 dx / dy / distance；
    - 过滤 ``distance <= 1e-4`` 或 ``text_value <= 0`` 的样本。
    """
    samples: List[DimSample] = []
    for e in msp.query("DIMENSION"):
        value = _parse_text(getattr(e.dxf, "text", None))
        if value is None or value <= 0:
            continue
        p2 = _point2(getattr(e.dxf, "defpoint2", None))
        p3 = _point2(getattr(e.dxf, "defpoint3", None))
        if p2 is None or p3 is None:
            continue
        dx = p3[0] - p2[0]
        dy = p3[1] - p2[1]
        dist = math.hypot(dx, dy)
        if dist <= _MIN_DISTANCE:
            continue
        samples.append(DimSample(
            text_value=value,
            measured_distance=dist,
            dx=dx,
            dy=dy,
            midpoint=((p2[0] + p3[0]) / 2.0, (p2[1] + p3[1]) / 2.0),
        ))
    return samples


# ---------------------------------------------------------------------------
# 标定
# ---------------------------------------------------------------------------

def _midpoint_in_region(mid: Tuple[float, float], region: dict) -> bool:
    """判断样本 midpoint 是否落在 region['region'] 内。

    契约要求通过 ``region['region'] = [x0, x1, y0, y1]`` 判断；region 缺失
    或非法时视为全图（匹配所有样本）。
    """
    reg = region.get("region")
    if reg is None:
        return True
    try:
        x0, x1, y0, y1 = reg
        x0, x1, y0, y1 = float(x0), float(x1), float(y0), float(y1)
    except (TypeError, ValueError):
        return True
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    x, y = mid
    return x0 <= x <= x1 and y0 <= y <= y1


def _round_to_common(scale: float) -> float:
    """把任意比例四舍五入到最接近的常见档位。

    例如 101.2 → 100，19.4 → 20，49.1 → 50。找不到接近档位（偏差过大）时
    原值返回，避免强拉到错误档位。
    """
    if scale <= 0:
        return scale
    best = min(_COMMON_SCALES, key=lambda s: abs(math.log(scale / s)))
    # 对数距离过大（相对偏差超过 30%）视为异常值，不强行归一
    if abs(math.log(scale / best)) > math.log(1.3):
        return scale
    return best


def _cluster_scale(samples: List[DimSample]) -> Optional[float]:
    """对一批同方向样本聚类，返回主视图 scale（或 None）。

    步骤：
    1. 计算每个样本的 ``scale = text_value / measured_distance``；
    2. 按 scale 聚类（相对容差 15% 内归为同档）；
    3. 主视图判定：优先选 text_value 最大的一批样本（text_value >=
       max_text * 0.5，且最多取 top-3）所在的 cluster；
    4. 返回该 cluster 内 scale 的中位数，再四舍五入到常见档位。
    """
    if not samples:
        return None

    raw = [s.text_value / s.measured_distance for s in samples]
    max_text = max(s.text_value for s in samples)
    # 主视图候选：大尺寸样本
    candidates = [s for s in samples if s.text_value >= max_text * _MAX_TEXT_RATIO]
    if len(candidates) > _TOP_N:
        candidates = sorted(candidates, key=lambda s: s.text_value, reverse=True)[:_TOP_N]

    # 对候选样本按 scale 聚类（一维贪心：与簇代表值相对偏差 <= 容差）
    clusters: List[Tuple[float, List[float]]] = []  # (代表值, 成员 scale 列表)
    for s in candidates:
        sc = s.text_value / s.measured_distance
        placed = False
        for rep, members in clusters:
            if abs(sc - rep) / max(abs(rep), 1e-6) <= _CLUSTER_TOLERANCE:
                members.append(sc)
                placed = True
                break
        if not placed:
            clusters.append((sc, [sc]))

    if not clusters:
        return None

    # 取规模最大的簇；规模相同时取代表值最大的（偏向主视图大比例）
    best_cluster = max(clusters, key=lambda c: (len(c[1]), c[0]))
    if not best_cluster[1]:
        return None
    return _round_to_common(median(best_cluster[1]))


def _split_direction(samples: List[DimSample]) -> Tuple[List[DimSample], List[DimSample]]:
    """按测量方向拆分：横向 (abs(dx) > abs(dy))、竖向 (abs(dy) >= abs(dx))。"""
    horizontal: List[DimSample] = []
    vertical: List[DimSample] = []
    for s in samples:
        if abs(s.dx) > abs(s.dy):
            horizontal.append(s)
        else:
            vertical.append(s)
    return horizontal, vertical


def calibrate_region_scales(
    samples: List[DimSample],
    regions: List[dict],
) -> List[dict]:
    """返回标定后的 regions 列表（浅拷贝 dict，更新 scale_x/scale_y 并记录元数据）。

    对每个 region：
    - 收集 midpoint 落在 region 内的样本；无样本则保持原 region 不变；
    - 横向/竖向分别判定主视图 scale；
    - 判定出有效值则更新 scale_x / scale_y，并记录
      ``_scale_x_calibrated`` / ``_scale_y_calibrated`` /
      ``_scale_origin='dimension_calibration'``。

    原始 regions 列表（以及每个 dict 的原始内容）不会被就地修改。
    """
    out: List[dict] = []
    for region in regions:
        if not isinstance(region, dict):
            # 防御：非 dict 项原样保留
            out.append(region)
            continue
        new = dict(region)
        if not samples:
            out.append(new)
            continue

        in_region = [s for s in samples if _midpoint_in_region(s.midpoint, region)]
        if not in_region:
            out.append(new)
            continue

        horizontal, vertical = _split_direction(in_region)
        scale_x = _cluster_scale(horizontal)
        scale_y = _cluster_scale(vertical)

        changed = False
        if scale_x is not None:
            new["scale_x"] = scale_x
            new["_scale_x_calibrated"] = True
            changed = True
        if scale_y is not None:
            new["scale_y"] = scale_y
            new["_scale_y_calibrated"] = True
            changed = True
        if changed:
            new["_scale_origin"] = "dimension_calibration"

        out.append(new)
    return out
