"""图纸意图四分类（Phase 2a）：每张分册属于哪种工程视图。

MLLM 只做意图分类、BOM 表格视觉识别、引线件号对齐（铁律）；
本模块产出的是「图纸意图」，不是 3D 坐标——坐标一律由确定性求解器
（_infer_assembly_views + scale_calibration）产出。

四类（与 goal Phase 2 措辞对齐）：
    * assembly_elevation_front  总装正立面（塔身/塔腿各段立面）
    * assembly_elevation_side   总装侧立面（与正立面同册并排时）
    * fabrication_detail        单件加工图（节点大样/角钢展开/装配详图）
    * plan_projection           水平投影（平面图/俯视塔脚排列）

    判据链：
    1. 文件名规则（classify_drawing_kind 既有链）：图签/材料表直接出局；
    2. MLLM 视觉判图（主判据，铁律允许——图纸意图分类）：
       渲染整册 PNG → 四分类 + 置信度 + 理由 + 正交视图判定；
       无 API/失败时回退 3 的几何判据（高召回优先，宁立面勿漏）；
    3. 线几何聚类（端点吸附 union-find）——连通分量宽高比/节拍层数/
       跨度比，作为 MLLM 不可用时的兜底与低置信复核佐证。

置信度（0~1）：MLLM 自报 + 确定性佐证一致性修正。
缓存：sheet_intent_cache(dir)。以 DXF 内容 hash 为键，
out/sheet_intent/ 下 JSON 缓存（不进库，.gitignore 已忽略）。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = REPO_ROOT / "out" / "sheet_intent"

INTENT_ASSEMBLY_FRONT = "assembly_elevation_front"
INTENT_ASSEMBLY_SIDE = "assembly_elevation_side"
INTENT_FABRICATION_DETAIL = "fabrication_detail"
INTENT_PLAN_PROJECTION = "plan_projection"

# 与 tower_spec.sheet_role 的映射（接线层用，本模块不 import，避免环）。
INTENT_TO_SHEET_ROLE = {
    INTENT_ASSEMBLY_FRONT: "elevation",
    INTENT_ASSEMBLY_SIDE: "elevation",
    INTENT_FABRICATION_DETAIL: "node_detail",
    INTENT_PLAN_PROJECTION: "plan",
}

# 立面主分量高宽比上限（细高塔身）；超过视为扁平网格（平面图/材料表区）。
_MAX_ELEVATION_ASPECT = 2.5
# 单件加工图判定：图面跨度 < 该比例 × 图册内最大立面跨度（同批比较）。
_FAB_RELATIVE_SPAN = 0.35
# 立面最少纵向节拍（水平线簇层数）。
_MIN_ELEVATION_BEATS = 3
# 连通分量内最少线数（少于视为零散标注线，不参与主分量竞争）。
_MIN_COMPONENT_LINES = 8
# 结构簇裁剪：纳入并集 bbox 的分量线数门槛（相对最大分量）与分量数上限。
_CROP_COMPONENT_RATIO = 0.3
_CROP_MAX_COMPONENTS = 6
# 连通域端点吸附容差（图面单位）：8 会把大样/图签辅助线粘进塔身簇
# （JC1-07 实测 tol=8 时塔段+右侧大样连成 422x331 一坨，MLLM 看到的
# 是一半塔一半大样）；4 恰好分开且全部 16 张塔视图簇保持完整。
_COMPONENT_TOL = 4.0
# 表格指纹：文本实体远多于结构线（材料表/图签版式）→ 直接按非立面处理。
_TABLE_TEXT_LINE_RATIO = 1.5
# 双线角钢指纹的下限阈值（低于判单线骨架）。
_DOUBLE_LINE_MIN = 0.10
# 缩微模型门：MLLM 判立面但「塔形簇」（aspect 带通）跨度 < 该比例 ×
# 同图册最大值 → 判缩微模型/节点大样（JC1-03 实测主簇 96x85 方形
# aspect=0.88，真立面簇 0.52~1.00），降级详图。
_MINIATURE_SPAN_RATIO = 0.35
# 塔形簇 aspect 带通：下限剔扁平网格（表格/平面），上限剔标注细长条
# （JC1-03 次簇 28x164 aspect=5.9）。实测塔视图簇 aspect 1.0~3.6。
_TOWER_CLUSTER_ASPECT = (0.9, 4.5)
# 特征缓存版本：判据链改动（如 components 增加 bbox）时递增，
# classify_batch_intents 对旧版本缓存按未命中处理（重算重写）。
_FEAT_VERSION = 2


@dataclass
class SheetIntent:
    """一张分册的意图判定结果（含全部判据留痕，可审计）。"""

    stem: str
    intent: str
    confidence: float
    reason: str
    features: Dict[str, Any] = field(default_factory=dict)
    filename_rule: Optional[Dict[str, Any]] = None
    mllm_review: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stem": self.stem,
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "features": self.features,
            "filename_rule": self.filename_rule,
            "mllm_review": self.mllm_review,
        }


class _UF:
    """端点吸附 union-find（与 scripts/classify_sheet_views.py 同思路）。"""

    def __init__(self) -> None:
        self.p: Dict[int, int] = {}

    def find(self, x: int) -> int:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _components_of(segs: List[Tuple[float, float, float, float]],
                   tol: float = 8.0) -> Dict[int, List[int]]:
    """端点吸附连通分量：返回 {root: [seg_idx...]}。"""
    uf = _UF()
    grid: Dict[Tuple[int, int], List[Tuple[int, Tuple[float, float]]]] = {}
    for i, (x0, y0, x1, y1) in enumerate(segs):
        for pt in ((x0, y0), (x1, y1)):
            grid.setdefault(
                (round(pt[0] / tol), round(pt[1] / tol)), []
            ).append((i, pt))
    for cell, items in grid.items():
        cx, cy = cell
        near = [
            item for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            for item in grid.get((cx + dx, cy + dy), [])
        ]
        for a, (i, pa) in enumerate(items):
            for j, pb in near[a + 1:]:
                if i != j and abs(pa[0] - pb[0]) <= tol and abs(pa[1] - pb[1]) <= tol:
                    uf.union(i, j)
    out: Dict[int, List[int]] = {}
    for i in range(len(segs)):
        out.setdefault(uf.find(i), []).append(i)
    return out


def _filename_intent(stem: str) -> Optional[Dict[str, Any]]:
    """文件名规则预判（图签/材料表出局；-01-1 轴测示意出局）。

    与 classify_drawing_kind 语义一致但只做「能确定出局」的部分；
    其余留给几何判据，避免文件名规则越权判立面/平面。
    """
    from .tower_dxf import classify_drawing_kind

    k = classify_drawing_kind(stem)
    kind = k["kind"]
    if kind == "title_block":
        return {"rule": "filename_title_block", "out": True,
                "intent": INTENT_FABRICATION_DETAIL, "confidence": 0.95,
                "detail": k["reason"]}
    if kind == "bom":
        return {"rule": "filename_bom", "out": True,
                "intent": INTENT_FABRICATION_DETAIL, "confidence": 0.95,
                "detail": k["reason"]}
    return None


def _sheet_line_features(dxf_path: Path) -> Dict[str, Any]:
    """提取一张图的矢量特征（结构线连通分量 + 尺寸标注 + 文本）。

    结构线用裸 modelspace LINE/LWPOLYLINE（不炸 INSERT——图框/标题栏
    块炸开会把 ~806x574 的图框线掺进分量与跨度统计）；文本/标注计数
    仍用炸开后的全实体（BOM 表文本在块内）。

    返回（全部为图面单位，比例无关的特征用比值/层数表达）：
        * components: 主分量列表 [{n, bbox, w, h, aspect, h_beats}]
        * max_span: 全图结构线总跨度（图面单位）
        * n_dim: DIMENSION 实体数
        * n_line: LINE/LWPOLYLINE 总数
        * n_text, n_numeric_text: TEXT 实体数 / 纯数字文本数
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()
    from .tower_dxf import _flatten_modelspace_entities

    segs: List[Tuple[float, float, float, float]] = _segment_cloud(dxf_path)
    n_line = len(segs)
    n_dim = 0
    n_text = 0
    n_numeric_text = 0
    for e in _flatten_modelspace_entities(msp):
        try:
            t = e.dxftype()
            if t == "DIMENSION":
                n_dim += 1
            elif t in ("TEXT", "MTEXT"):
                n_text += 1
                try:
                    s = (e.dxf.text if t == "TEXT" else e.text).strip()
                except Exception:
                    s = ""
                if s and any(ch.isdigit() for ch in s):
                    n_numeric_text += 1
        except Exception:
            continue

    comps_out: List[Dict[str, Any]] = []
    max_span = 0.0
    if segs:
        comps = _components_of(segs, tol=_COMPONENT_TOL)
        for idxs in comps.values():
            if len(idxs) < _MIN_COMPONENT_LINES:
                continue
            xs = [c for i in idxs for c in (segs[i][0], segs[i][2])]
            ys = [c for i in idxs for c in (segs[i][1], segs[i][3])]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            w = x1 - x0
            h = y1 - y0
            max_span = max(max_span, w, h)
            # 纵向节拍：按 y 网格统计水平线（|dy| 小）出现层数。
            ys_of_horizontal = sorted({
                round((segs[i][1] + segs[i][3]) / 2)
                for i in idxs
                if abs(segs[i][1] - segs[i][3]) < 1e-6
                and abs(segs[i][0] - segs[i][2]) > 0.05 * max(w, 1.0)
            })
            beats = 0
            last = None
            for y in ys_of_horizontal:
                if last is None or y - last > 3.0:
                    beats += 1
                    last = y
            comps_out.append({
                "n": len(idxs), "w": round(w, 1), "h": round(h, 1),
                "aspect": round(h / w, 3) if w > 1e-6 else None,
                "h_beats": beats,
                "bbox": [round(x0, 1), round(x1, 1), round(y0, 1), round(y1, 1)],
            })
        comps_out.sort(key=lambda c: -c["n"])

    return {
        "feat_version": _FEAT_VERSION,
        "components": comps_out[:8],
        "max_span": round(max_span, 1),
        "n_line": n_line, "n_dim": n_dim,
        "n_text": n_text, "n_numeric_text": n_numeric_text,
    }


def _classify_by_features(
    stem: str,
    feats: Dict[str, Any],
    batch_max_span: float,
) -> Tuple[str, float, str]:
    """几何特征判据（核心）。batch_max_span 为同图册立面最大跨度参照。"""
    comps = feats.get("components") or []
    if not comps:
        return (INTENT_FABRICATION_DETAIL, 0.4,
                "无有效结构连通分量（≥8 线），按非立面处理")
    main = comps[0]
    aspect = main.get("aspect") or 0.0
    beats = int(main.get("h_beats") or 0)
    span = max(main.get("w") or 0.0, main.get("h") or 0.0)

    # 平面投影：近方形 + 无纵向节拍（水平线簇）+ 多分量（塔脚排列）。
    if (aspect is not None and 0.5 <= aspect <= 1.8 and beats == 0
            and len(comps) >= 2):
        return (INTENT_PLAN_PROJECTION, 0.65,
                f"主分量近方形(aspect={aspect}) 且无纵向节拍，多分量塔脚排列")

    # 立面：细高 + ≥3 层纵向节拍。
    if aspect and aspect < _MAX_ELEVATION_ASPECT and beats >= _MIN_ELEVATION_BEATS:
        rel = (span / batch_max_span) if batch_max_span > 0 else 1.0
        if rel >= _FAB_RELATIVE_SPAN:
            return (INTENT_ASSEMBLY_FRONT, 0.7,
                    f"细高主分量(aspect={aspect}) 纵向节拍 {beats} 层，"
                    f"跨度达图册立面 {rel:.0%}")

    # 单件加工图：跨度小 或 无多层节拍。
    rel = (span / batch_max_span) if batch_max_span > 0 else 1.0
    if rel < _FAB_RELATIVE_SPAN:
        return (INTENT_FABRICATION_DETAIL, 0.7,
                f"主分量跨度仅为图册立面 {rel:.0%}（<{_FAB_RELATIVE_SPAN:.0%}），单件图特征")
    if beats < _MIN_ELEVATION_BEATS:
        return (INTENT_FABRICATION_DETAIL, 0.55,
                f"纵向节拍仅 {beats} 层（<{_MIN_ELEVATION_BEATS}），无多层结构")
    return (INTENT_FABRICATION_DETAIL, 0.5,
            f"主分量 aspect={aspect} 不满足立面特征，按加工图保守处理")


def _tower_cluster_span(feats: Dict[str, Any]) -> float:
    """该页「塔形簇」跨度：显著分量里 aspect 带通内簇的最大跨度。

    与 max_span 的区别：max_span 计入表格外框/图签（01-2 的 BOM 网格
    771 图面单位）与扁平网格；塔形簇口径只认「细高适中」的**显著**簇
    （线数 ≥ 最大簇 30%，剔除零散标注/引出线小簇——JC1-03 的 9 线
    120x296 细长簇不参与），节点大样主簇（方形 96x85）、标注细长条
    （28x164）都被排除。
    """
    comps = feats.get("components") or []
    if not comps:
        return 0.0
    top_n = float(comps[0].get("n") or 0)
    if top_n <= 0:
        return 0.0
    threshold = max(_MIN_COMPONENT_LINES, _CROP_COMPONENT_RATIO * top_n)
    span = 0.0
    lo, hi = _TOWER_CLUSTER_ASPECT
    for c in comps:
        if float(c.get("n") or 0) < threshold:
            continue
        aspect = c.get("aspect")
        if aspect is None:
            continue
        if lo <= float(aspect) <= hi:
            span = max(span, float(c.get("w") or 0), float(c.get("h") or 0))
    return span


def _text_corroboration(intent: str, feats: Dict[str, Any]) -> float:
    """文本佐证：加工图有高密度数字标注（孔距/件号）。返回 ±修正量。"""
    n_text = feats.get("n_text") or 0
    n_num = feats.get("n_numeric_text") or 0
    if n_text <= 0:
        return 0.0
    num_ratio = n_num / n_text
    n_dim = feats.get("n_dim") or 0
    if intent == INTENT_FABRICATION_DETAIL:
        if num_ratio >= 0.5 and n_dim >= 20:
            return +0.2
        if num_ratio >= 0.5:
            return +0.1
    else:
        if num_ratio >= 0.8 and n_dim >= 40:
            return -0.1  # 大量尺寸标注更像加工图
    return 0.0


# ---------------------------------------------------------------------------
# MLLM 视觉判图（Phase 2b 主判据）
# ---------------------------------------------------------------------------

_INTENT_ENUM = (INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE,
                INTENT_FABRICATION_DETAIL, INTENT_PLAN_PROJECTION)

_INTENT_AGENT_PROMPT = """你是输电铁塔工程图纸的意图分类器。判断这张图纸（结构线密集区放大图）的主视图类型，输出 JSON。

四类（只选一个）：
- assembly_elevation_front: 铁塔塔身/塔段的正立面视图——主材+横杆+斜杆
  多层交叉的格构塔身，常带高度标注
- assembly_elevation_side: 侧立面视图，或一张图上正立面+侧立面双塔形并排
- fabrication_detail: 单件加工图——单根角钢/节点板的孔位标注详图，
  小而精细；也含节点大样/装配详图/轴测示意
- plan_projection: 水平投影图——俯视塔脚排列/平面布置，近方形轮廓

判图口径（重要）：
A. 放大图可能同时裁入多个视图区域（多段塔身并排、塔身+旁边的大样）。
   只要图中**存在任一**格构塔身立面视图（竖向主材+≥3层横杆斜杆交叉，
   通常占图面较大块），就判 assembly_elevation_*（front 或 side 按
   塔形数量/朝向选一个），不要因为旁边混入节点大样或俯视小图而改判
   fabrication_detail / plan_projection。
B. 只有当图中完全没有任何格构塔身立面（只有角钄件详图/孔位标注/
   轴测示意/纯平面布置）时，才判 fabrication_detail / plan_projection。

严格要求：
1. 只输出 JSON，不要任何解释文字。
2. is_ortho_projection: 图中主结构是否按正交投影画（立面/平面是，轴测/透视不是）。
3. has_elevation_like_tower_structure: 是否有塔状分层轮廓（主材+多层横斜杆）。
4. 拿不准时如实降低 confidence。
5. 输出格式：
{"intent": "<四类之一>", "confidence": 0.85,
 "is_ortho_projection": true, "has_elevation_like_tower_structure": true,
 "reason": "简短中文理由（20字内）"}
"""

_INTENT_AGENT_SCHEMA = {
    "type": "object",
    "required": ["intent", "confidence", "reason"],
    "properties": {
        "intent": {"type": "string", "enum": list(_INTENT_ENUM)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "is_ortho_projection": {"type": "boolean"},
        "has_elevation_like_tower_structure": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}


def _segment_cloud(dxf_path: Path) -> List[Tuple[float, float, float, float]]:
    """LINE + LWPOLYLINE 展开为线段云（modelspace 直接实体，不炸 INSERT）。

    不炸块：GWTKA1 之类的图框/标题栏块炸开后会与标注线粘连，把 806x574
    的图框连同图签当成「主结构」；塔身结构线在两张塔（JC1/ZC1）的
    全部分册上都是裸 LINE/LWPOLYLINE。
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    segs: List[Tuple[float, float, float, float]] = []
    for e in doc.modelspace():
        try:
            t = e.dxftype()
            if t == "LINE":
                segs.append((e.dxf.start.x, e.dxf.start.y,
                             e.dxf.end.x, e.dxf.end.y))
            elif t == "LWPOLYLINE":
                pts = list(e.get_points("xy"))
                for i in range(len(pts) - 1):
                    segs.append((pts[i][0], pts[i][1],
                                 pts[i + 1][0], pts[i + 1][1]))
        except Exception:
            continue
    return segs


def _structural_cluster_bbox(dxf_path: Path) -> Optional[Tuple[float, float, float, float]]:
    """主结构簇 bbox（图面坐标）：显著结构分量的并集包围盒。

    整页渲染会把塔身缩成图钉大小（ZC1 单件加工图版式实测 MLLM 误判），
    裁剪放大后 MLLM 才能看清主材/横杆/斜杆纹理。多视图分册（ZC1-10
    实测：正立面+侧立面+俯视图三簇同量级并排）只裁最大分量会丢视图，
    所以取「线数 ≥ 最大分量 30%」的全部显著分量（上限 6 个）的并集——
    JC1/ZC1 全部 16 张实测该并集与 overlay 声明的塔视图区域对齐；
    图框/标题栏簇（GWTKA1 块，~806x574 图面单位）因不炸块天然不入云。
    """
    segs = _segment_cloud(dxf_path)
    if len(segs) < 50:
        return None
    comps = _components_of(segs, tol=_COMPONENT_TOL)
    ranked = sorted(
        (len(idxs) for idxs in comps.values() if len(idxs) >= _MIN_COMPONENT_LINES),
        reverse=True)
    if not ranked:
        return None
    keep_threshold = max(_MIN_COMPONENT_LINES, _CROP_COMPONENT_RATIO * ranked[0])
    kept = sorted(
        (idxs for idxs in comps.values()
         if len(idxs) >= keep_threshold),
        key=len, reverse=True)[:_CROP_MAX_COMPONENTS]
    xs: List[float] = []
    ys: List[float] = []
    for idxs in kept:
        for i in idxs:
            xs.extend((segs[i][0], segs[i][2]))
            ys.extend((segs[i][1], segs[i][3]))
    return (min(xs), min(ys), max(xs), max(ys))


def _double_line_ratio(dxf_path: Path) -> Optional[float]:
    """主连通域的短碎线占比：双线角钢的确定性指纹。

    真实分段立面/总装图用双线画角钢（一根杆 = 两条平行长线 + 大量
    短碎线/填充线，实测短碎线占比 0.56~0.72）；单线图/示意图是
    单线骨架（占比 ~0.01）。用于复核 MLM 的立面判定——
    MLLM 说「正立面」但无双线指纹时降级为 fabrication_detail
    （多呼高单线图 01-1 实测会骗过 MLLM 的视觉判定）。
    """
    segs = _segment_cloud(dxf_path)
    if len(segs) < 50:
        return None
    comps = _components_of(segs, tol=_COMPONENT_TOL)
    best = max(comps.values(), key=len)
    if len(best) < 30:
        return None
    xs = [c for i in best for c in (segs[i][0], segs[i][2])]
    ys = [c for i in best for c in (segs[i][1], segs[i][3])]
    ref = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    short = 0
    for i in best:
        x0, y0, x1, y1 = segs[i]
        span = max(abs(x1 - x0), abs(y1 - y0))
        # 双线角钢的碎线尺度是「主结构跨度的 1% 量级」：按分量自身
        # 尺度归一，跨塔型可比（不写死图纸单位阈值）。
        if span < 0.01 * ref:
            short += 1
    return short / len(best)


def _render_structural_crop(dxf_path: Path, png_path: Path,
                            bbox: Tuple[float, float, float, float]) -> None:
    """按 bbox（外扩 15%）渲染放大图。"""
    import matplotlib

    matplotlib.use("Agg")
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    import matplotlib.pyplot as plt

    x0, y0, x1, y1 = bbox
    px, py = (x1 - x0) * 0.15, (y1 - y0) * 0.15
    doc = ezdxf.readfile(str(dxf_path))
    ctx = RenderContext(doc)
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE)
    fig = plt.figure(figsize=(14, 14), dpi=110)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_aspect("equal")
    backend = MatplotlibBackend(ax)
    Frontend(ctx, backend, config=cfg).draw_layout(doc.modelspace())
    ax.set_xlim(x0 - px, x1 + px)
    ax.set_ylim(y0 - py, y1 + py)
    ax.axis("off")
    fig.savefig(str(png_path), bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)


def _mllm_review_intent(
    dxf_path: Path,
    cache_dir: Path,
    mllm: Any,
) -> Optional[Dict[str, Any]]:
    """结构簇放大渲染 → MLLM 四分类。结果按 DXF hash 缓存。

    MLLM 只输出意图标签与置信度（铁律：不产 3D 坐标）。
    失败/不可用返回 None（调用方回退确定性几何判据）。
    """
    h = hashlib.sha256(dxf_path.read_bytes()).hexdigest()[:24]
    verdict_path = cache_dir / f"{dxf_path.stem}__{h}__mllm.json"
    if verdict_path.exists():
        try:
            return json.loads(verdict_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        png_path = cache_dir / f"{dxf_path.stem}__{h}.png"
        bbox = _structural_cluster_bbox(dxf_path)
        if bbox is not None:
            _render_structural_crop(dxf_path, png_path, bbox)
        else:
            from .hybrid_dxf_agent import render_dxf_preview_with_mapping

            render_dxf_preview_with_mapping(dxf_path, png_path, dpi=140)
        parsed, meta = mllm.call_agent_json(
            _INTENT_AGENT_PROMPT, str(png_path), _INTENT_AGENT_SCHEMA,
            agent="sheet_intent",
        )
    except Exception:
        return None
    if not parsed:
        return None
    verdict = {
        "intent": str(parsed.get("intent")),
        "confidence": float(parsed.get("confidence") or 0.0),
        "is_ortho_projection": bool(parsed.get("is_ortho_projection")),
        "has_elevation_like_tower_structure": bool(
            parsed.get("has_elevation_like_tower_structure")),
        "reason": str(parsed.get("reason") or ""),
        "model": getattr(mllm, "model", None),
        "provider": getattr(mllm, "provider", None),
    }
    try:
        verdict_path.write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return verdict


def classify_sheet_intent(
    dxf_path: str | Path,
    *,
    batch_max_span: float = 0.0,
) -> SheetIntent:
    """对一张分册做四分类（确定性，不调 API）。"""
    dxf_path = Path(dxf_path)
    stem = dxf_path.stem
    fn = _filename_intent(stem)
    if fn and fn.get("out"):
        return SheetIntent(
            stem=stem, intent=fn["intent"], confidence=fn["confidence"],
            reason=f"文件名规则出局：{fn.get('detail')}",
            filename_rule=fn,
        )
    try:
        feats = _sheet_line_features(dxf_path)
    except Exception as exc:
        return SheetIntent(
            stem=stem, intent=INTENT_FABRICATION_DETAIL, confidence=0.2,
            reason=f"DXF 读取失败（{type(exc).__name__}: {exc}），保守按非立面",
            filename_rule=fn,
        )
    intent, conf, reason = _classify_by_features(stem, feats, batch_max_span)
    conf = min(1.0, max(0.0, conf + _text_corroboration(intent, feats)))
    return SheetIntent(
        stem=stem, intent=intent, confidence=conf, reason=reason,
        features=feats, filename_rule=fn,
    )


def classify_batch_intents(
    dxf_paths: List[str | Path],
    *,
    cache_dir: Optional[str | Path] = None,
    mllm: Any = None,
    use_mllm: bool = True,
) -> Dict[str, SheetIntent]:
    """批量分类：MLLM 视觉判图为主，确定性几何为佐证/兜底。

    流程：
        1. 文件名规则出局（图签/材料表）——零成本，不经 MLLM；
        2. 其余每张：MLLM 视觉四分类（可用时；结果按 DXF hash 缓存）；
        3. MLLM 缺失/失败 → 确定性几何判据兜底（置信度标注 fallback）；
        4. 置信度融合：MLLM 自报置信度 + 几何佐证一致性微调。

    缓存以 DXF 内容 sha256 为键（改图自动失效）；缓存命中不重复解析。
    """
    dxf_paths = [Path(p) for p in dxf_paths]
    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    if use_mllm and mllm is None:
        try:
            from .mllm_backend import MLLMBackend

            mllm = MLLMBackend()
        except Exception:
            mllm = None
    mllm_ok = use_mllm and mllm is not None and getattr(
        mllm, "available", lambda: False)()

    # 第一遍：特征（带缓存）——几何佐证与兜底共用。
    # 缓存版本不匹配（判据链/特征字段演进）按未命中处理，重算重写。
    feats_by_stem: Dict[str, Dict[str, Any]] = {}
    for p in dxf_paths:
        cp = cache_dir / f"{p.stem}__{_content_hash(p)}.json"
        if cp.exists():
            try:
                cached = json.loads(cp.read_text(encoding="utf-8"))
                if cached.get("feat_version") == _FEAT_VERSION:
                    feats_by_stem[p.stem] = cached
                    continue
            except Exception:
                pass
        feats = _sheet_line_features(p)
        feats_by_stem[p.stem] = feats
        try:
            cp.write_text(json.dumps(feats, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # 图册塔形簇跨度参照：全部页「塔形簇」最大跨度（缩微模型门的
    # 分母——同一图册内的相对尺度是确定性判据，不受渲染缩放影响）。
    batch_tower_span = 0.0
    for feats in feats_by_stem.values():
        batch_tower_span = max(batch_tower_span, _tower_cluster_span(feats))

    # 第二遍：判意图。
    out: Dict[str, SheetIntent] = {}
    for p in dxf_paths:
        stem = p.stem
        fn = _filename_intent(stem)
        if fn and fn.get("out"):
            out[stem] = SheetIntent(
                stem=stem, intent=fn["intent"], confidence=fn["confidence"],
                reason=f"文件名规则出局：{fn.get('detail')}",
                filename_rule=fn,
            )
            continue

        geo_intent, geo_conf, geo_reason = _classify_by_features(
            stem, feats_by_stem[stem], batch_tower_span)
        geo_conf = min(1.0, max(0.0, geo_conf + _text_corroboration(
            geo_intent, feats_by_stem[stem])))

        verdict: Optional[Dict[str, Any]] = None
        if mllm_ok:
            verdict = _mllm_review_intent(p, cache_dir, mllm)

        # 确定性复核：双线角钢指纹。MLLM 判立面但主连通域无双线碎线
        # （单线图/示意图骨架）→ 降级 fabrication_detail。
        # JC1-01-1（多呼高单线图）实测 MLLM 会误判 front，此门拦截。
        dbl = _double_line_ratio(p)

        if verdict and verdict.get("intent") in _INTENT_ENUM:
            intent = str(verdict["intent"])
            conf = float(verdict.get("confidence") or 0.5)
            reason = f"MLLM 视觉判图：{verdict.get('reason') or ''}"
            # 表格指纹复核：文本实体远多于结构线（材料表/图签版式，
            # JC1-01-2 实测 text/line≈5.9，真立面页 0.3~0.9）→ MLLM
            # 在密密麻麻的数字矩阵上看不出结构，判成平面/详图均不可信，
            # 按非立面（表格）处理。
            feats = feats_by_stem[stem]
            n_text = feats.get("n_text") or 0
            n_line = feats.get("n_line") or 0
            table_signature = (n_line > 0 and n_text > 500
                               and n_text / n_line > _TABLE_TEXT_LINE_RATIO)
            if table_signature and intent in (
                    INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE,
                    INTENT_PLAN_PROJECTION):
                intent = INTENT_FABRICATION_DETAIL
                conf = 0.85
                reason += (f"；表格指纹（text/line={n_text / n_line:.1f}，"
                           "标注实体远多于结构线），判定为材料表/图签版式")
                verdict = dict(verdict, overridden_by="table_signature")
            elif dbl is not None and dbl < _DOUBLE_LINE_MIN and intent in (
                    INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE):
                intent = INTENT_FABRICATION_DETAIL
                conf = max(0.6, conf - 0.2)
                reason += (
                    f"；双线角钢指纹缺失（短碎线占比 {dbl:.2f} < "
                    f"{_DOUBLE_LINE_MIN:.2f}），判定为单线/示意图骨架，复核降级")
                verdict = dict(verdict, overridden_by="double_line_gate")
            elif (intent in (INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE)
                  and batch_tower_span > 0):
                # 缩微模型门：格构节点大样放大后确实「像塔身」（JC1-03
                # 实测 MLLM 在多视图口径下判了 front），但该页「塔形簇」
                # 跨度仅为图册最大值的 24%（真立面 52%~100%）——同图册
                # 相对尺度是确定性判据，不受渲染缩放影响。塔形簇为零
                # （无 aspect 带通内的细高簇）同样降级。
                tower_span = _tower_cluster_span(feats_by_stem[stem])
                rel = tower_span / batch_tower_span
                if rel < _MINIATURE_SPAN_RATIO:
                    intent = INTENT_FABRICATION_DETAIL
                    conf = 0.8
                    reason += (
                        f"；缩微模型门（塔形簇跨度仅为图册立面 {rel:.0%} < "
                        f"{_MINIATURE_SPAN_RATIO:.0%}），判定为节点大样/缩微模型")
                    verdict = dict(verdict, overridden_by="miniature_gate")
                else:
                    if intent == geo_intent:
                        conf = min(1.0, conf + 0.1)
                    if dbl is not None and dbl >= _DOUBLE_LINE_MIN and intent in (
                            INTENT_ASSEMBLY_FRONT, INTENT_ASSEMBLY_SIDE):
                        conf = min(1.0, conf + 0.05)
            elif (intent in (INTENT_FABRICATION_DETAIL, INTENT_PLAN_PROJECTION)
                  and not table_signature):
                # 确定性立面反证：MLLM 判非立面但证据链压倒——双线角钢
                # 指纹强（真分段立面 0.36~0.72；单线图 ~0.02）+ 塔形簇
                # 跨度达图册显著比例 + 主簇线数充足。MLLM 在多视图/混合
                # 版面上 run 间波动会漏判立面（06/07/10/12 实测），
                # 确定性证据不波动。conf 保守 0.7 并留痕复核来源。
                tower_span = _tower_cluster_span(feats_by_stem[stem])
                rel = (tower_span / batch_tower_span) if batch_tower_span > 0 else 0.0
                main_n = 0
                comps = feats_by_stem[stem].get("components") or []
                if comps:
                    main_n = int(comps[0].get("n") or 0)
                if (dbl is not None and dbl >= 0.30 and rel >= 0.5
                        and main_n >= 100):
                    intent = INTENT_ASSEMBLY_FRONT
                    conf = 0.7
                    reason += (
                        f"；确定性立面证据压倒 MLLM 反判：双线角钢指纹 "
                        f"{dbl:.2f} + 塔形簇跨度 {rel:.0%} + 主簇 {main_n} 线"
                        "（复核反证）")
                    verdict = dict(verdict, overridden_by="elevation_evidence")
            if verdict.get("is_ortho_projection") is False:
                reason += "（非正交投影→详图降级）"
            out[stem] = SheetIntent(
                stem=stem, intent=intent, confidence=conf, reason=reason,
                features=feats_by_stem[stem], filename_rule=fn,
                mllm_review=verdict,
            )
        elif mllm_ok and verdict is not None:
            # MLLM 返回的 intent 不在枚举（异常输出）→ 几何兜底。
            out[stem] = SheetIntent(
                stem=stem, intent=geo_intent, confidence=geo_conf,
                reason=(f"几何判据兜底（MLLM 输出异常："
                        f"{verdict.get('intent')!r}）：{geo_reason}"),
                features=feats_by_stem[stem], filename_rule=fn,
                mllm_review=verdict,
            )
        else:
            out[stem] = SheetIntent(
                stem=stem, intent=geo_intent, confidence=geo_conf,
                reason=f"几何判据兜底（MLLM 不可用）：{geo_reason}",
                features=feats_by_stem[stem], filename_rule=fn,
            )
    return out


def _content_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:24]


def side_elevation_split(
    intents: Dict[str, SheetIntent],
) -> Dict[str, SheetIntent]:
    """front→side 二分：同一张图上正立面+侧立面并排时（_infer_assembly_views
    的 x 中位切分已在 region 层做），此处由调用方在接线层处理——
    本函数把「同册双簇」的 front 标注为 front+side 双视图意图
    （features.components 前两名宽高比接近、横向分离）。

    判据全部来自几何：主分量与次分量横向分离（gap > 簇内展宽×0.5）
    且两者都满足立面特征。
    """
    for stem, si in intents.items():
        if si.intent != INTENT_ASSEMBLY_FRONT:
            continue
        comps = (si.features or {}).get("components") or []
        if len(comps) < 2:
            continue
        a, b = comps[0], comps[1]
        # 近似判断：两分量线数同量级 + 各自都细高有节拍。
        if (a.get("h_beats", 0) >= _MIN_ELEVATION_BEATS
                and b.get("h_beats", 0) >= _MIN_ELEVATION_BEATS
                and 0.3 <= (b.get("n", 0) / max(a.get("n", 1), 1)) <= 3.0
                and a.get("aspect") and b.get("aspect")
                and a["aspect"] < _MAX_ELEVATION_ASPECT
                and b["aspect"] < _MAX_ELEVATION_ASPECT):
            si.intent = INTENT_ASSEMBLY_SIDE
            si.reason = (
                f"front→side 双立面并排：主/次分量线数 {a.get('n')}/{b.get('n')}，"
                "节拍双层（正立面+侧立面同册，_infer_assembly_views 会切 x 中位）")
    return intents
