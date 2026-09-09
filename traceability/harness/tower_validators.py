"""铁塔专用验证器（方案 §4）。

五条规则：
    * r_topology_closed      每根杆件两端节点存在
    * r_bom_length_match     杆件 3D 长度 vs BOM 长度，偏差 ≤ 3%
    * r_bom_section_match    杆件截面 vs BOM 截面，归一化后相等
    * r_node_fully_solved    关键节点三轴坐标已知（无 placeholder）
    * r_no_duplicate_bar_id  杆件编号唯一
"""

from __future__ import annotations

import math
import re
from typing import Optional

from ..model import EngineeringModel, ValidationStatus
from .harness import ValidationResult


def _normalize_section(s: str) -> str:
    """截面规格归一化：L100x8 / L100×8 / L100*8 -> l100x8"""
    return re.sub(r"[×*xX]", "x", s.strip().lower().replace(" ", ""))


def _iter_bars(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_bar":
            yield cid, comp


def _iter_nodes(model: EngineeringModel):
    for cid, comp in model.components.items():
        if comp.kind == "tower_node":
            yield cid, comp


def validate_topology_closed(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """每根杆件两端节点必须存在。"""
    node_ids = {cid for cid, _ in _iter_nodes(model)}
    missing = []
    for cid, bar in _iter_bars(model):
        for end in ("from_node", "to_node"):
            nid = bar.properties.get(end)
            if nid not in node_ids:
                missing.append(f"{bar.properties.get('bar_id', cid)}.{end}={nid}")
    if not missing:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "所有杆件两端节点存在", "topology-closed")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"{len(missing)} 处拓扑断裂：{missing[:5]}", "topology-closed")


def _physical_stem(cid: str) -> str:
    """组件 id → 物理杆 stem（四面展开前的身份）。

    展开组件形如 ``4f_<stem>_F``（面后缀 F/B/L/R）。四面实例共享同一
    stem：front 实例是识别源头，B/L/R 是镜像。剥掉面后缀得到 stem。
    """
    if cid.startswith("4f_"):
        cid = cid[3:]
    for suf in ("_F", "_B", "_L", "_R"):
        if cid.endswith(suf):
            return cid[: -len(suf)]
    return cid


def _chain_span_mm(model: "EngineeringModel", segs: list) -> Optional[float]:
    """split 链端到端距离（3D 节点坐标的最远两点）。

    链加和会把 T 接打断产生的重叠段重复计量（606 实证：同一棱上
    6 段互相 T 接，加和 9348 vs span 2337）；分段画线的物理母杆长度
    是端到端距离。节点坐标缺失返回 None，调用方退化为段长加和。
    """
    pts: list = []
    for _cid, _bar, _len, _bom in segs:
        for end in ("from_node", "to_node"):
            nid = _bar.properties.get(end)
            node = model.components.get(str(nid)) if nid else None
            if node is None:
                continue
            p = node.properties or {}
            try:
                pts.append((float(p["x"]), float(p["y"]), float(p["z"])))
            except (TypeError, KeyError):
                continue
    if not pts:
        return None
    best = 0.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = ((pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2
                 + (pts[i][2] - pts[j][2]) ** 2) ** 0.5
            if d > best:
                best = d
    return best


def validate_bom_length_match(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """杆件 3D 长度 vs BOM 长度，偏差 ≤ 3%。

    2026-08-31 前的旧口径把四面展开的每个实例当独立杆核验——同一物理杆的
    F/B/L/R 四个镜像实例被计 4 次（319 根「超差」里 4/5 是重复计数）。
    修正后的核验对象：

        物理杆 = 同一 stem 的四面实例取 front 代表（无 front 取任一面），
        B/L/R 镜像不再单独计数。

    split 链加和口径（P2，2026-09-08）：图纸分段画线（制造分段/视图
    分段）的 BOM 母杆核验以**链**为单位——

        核验对象 = front 面同一 root_bar_id（无 root 用剥面 stem）的全部
        段；单段链用段长，多段链用端到端 span（不叠加——T 接打断的
        重叠段加和会重复计量，606 实证：6 段互相 T 接，加和 9348 vs
        span 2337）。

        链 span < BOM = 欠识别（识别覆盖缺口，Phase 2/3 主战场）→ PENDING
        不拦交付（与 r_project_bom_master 的 under_identified 同语义）；
        链 span > BOM = 件号错挂或母杆未截断重复（Phase 4.3）→ suspect
        已标（bar_id_length_suspect）或 bar_id_dup/非 primary（同视图
        重复标注的次实例）走 review 队列；无标记的纯超差才 FAILED
        （诚实失败）。

    消息带物理杆/实例两种口径计数，便于审计对比。
    """
    failures = []
    under = []    # 欠识别：链 span < BOM 长（识别覆盖缺口，非数据矛盾）
    suspect = []  # 口径存疑：P4.3 已标 bar_id_length_suspect / bar_id_dup
                  # 非 primary（同号歧义/重复标注次实例），review 队列
    matched = 0
    instance_count = 0
    # 链分组：(root_bar_id | 剥面 stem, face) → [(cid, bar)]
    chains: dict = {}
    for cid, bar in _iter_bars(model):
        bid = bar.properties.get("bar_id")
        bom_dim = model.dimensions.get(f"dim_bom_length_{bid}")
        if bom_dim is None or bom_dim.value is None:
            continue
        # 跨视图合并后优先用 3D 长度；未合并的投影杆件用图纸投影长度
        actual = bar.properties.get("length_mm_3d")
        if actual is None:
            actual = bar.properties.get("length_mm")
        if actual is None:
            continue
        bom_len = float(bom_dim.value)
        if bom_len <= 0:
            continue
        instance_count += 1
        root = bar.properties.get("root_bar_id") or _physical_stem(cid)
        face = str(bar.properties.get("face") or "")
        chains.setdefault((root, face), []).append((cid, bar, float(actual), bom_len))
    for (root, face), segs in chains.items():
        # front 是识别源头，优先作为物理杆代表（无 front 的链单独核验）
        if face != "f" and any(f == "f" for (_r, f) in chains if _r == root):
            continue
        bid = segs[0][1].properties.get("bar_id")
        bom_len = segs[0][3]
        if len(segs) == 1:
            actual = segs[0][2]
        else:
            actual = _chain_span_mm(model, segs)
            if actual is None:  # 节点坐标缺失 → 退化为段长加和
                actual = sum(s[2] for s in segs)
        matched += 1
        dev = abs(actual - bom_len) / bom_len
        if dev <= 0.03:
            continue
        rec = (bid, round(actual, 1), round(bom_len, 1), round(dev * 100, 1))
        _sus = (bool(segs[0][1].properties.get("bar_id_length_suspect"))
                or bool(segs[0][1].properties.get("bar_id_dup")))
        if actual < bom_len:
            under.append(rec)
        elif _sus:
            suspect.append(rec)
        else:
            failures.append(rec)
    if matched == 0:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "无足够数据做 BOM 长度核验", "bom-length")
    if failures:
        return ValidationResult(
            rule_id, ValidationStatus.FAILED,
            f"{len(failures)} 根物理杆长度超差（旧实例口径 {instance_count}）："
            f"{sorted(failures, key=lambda t: -t[3])[:3]}"
            + (f"；另有 {len(under)} 根欠识别" if under else "")
            + (f"、{len(suspect)} 根口径存疑（review 队列）" if suspect else ""),
            "bom-length")
    if under or suspect:
        # V1 同语义（r_project_bom_master）：欠识别是召回缺口、suspect 是
        # P4.3 已登记的人工复核口径歧义（bar_id_length_suspect / bar_id_dup
        # 同视图重复标注次实例），均非可自动裁决的数据矛盾 → PENDING
        # 不拦交付；无标记的纯超差（件号错挂/重复）才 FAILED。
        return ValidationResult(
            rule_id, ValidationStatus.PENDING,
            f"{matched} 核验 / {len(under)} 根欠识别（链 span < BOM 母杆，"
            f"Phase 2/3 主战场）/ {len(suspect)} 根口径存疑（suspect/dup 已标，"
            f"review 队列；旧实例口径 {instance_count}）："
            f"{sorted(suspect, key=lambda t: -t[3])[:3]}",
            "bom-length")
    return ValidationResult(rule_id, ValidationStatus.PASSED,
                            f"{matched} 根物理杆长度与 BOM 偏差 ≤ 3%"
                            f"（展开实例 {instance_count}）", "bom-length")


def validate_bom_section_match(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """杆件截面 vs BOM 截面。

    P5 约束残差（2026-09-03）：件号挂接已判 suspect（bar_id_length_suspect，
    P4.3 review 队列）的杆，其截面矛盾与长度矛盾是**同一处挂接错乱**的
    两个症状——计入 review 计数（不重复引爆 FAILED），与
    r_bom_length_match 的 suspect 语义对齐。截面是角钢对角钢的实质
    不符且无 suspect 标记 → 仍然 FAILED（诚实失败）。
    """
    mismatch = []
    suspect = []
    matched = 0
    for cid, bar in _iter_bars(model):
        bid = bar.properties.get("bar_id")
        bom_dim = model.dimensions.get(f"dim_bom_section_{bid}")
        actual = bar.properties.get("section")
        if bom_dim is None or bom_dim.value is None or actual is None:
            continue
        matched += 1
        if _normalize_section(str(actual)) != _normalize_section(str(bom_dim.value)):
            if bar.properties.get("bar_id_length_suspect") or (
                    bar.properties.get("bar_id") is None):
                # 已在长度阶梯的 review 队列（同源挂接错乱）→ 合并计数
                suspect.append((bid, actual, bom_dim.value))
            else:
                mismatch.append((bid, actual, bom_dim.value))
    if matched == 0:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "无足够数据做 BOM 截面核验", "bom-section")
    if mismatch:
        return ValidationResult(rule_id, ValidationStatus.FAILED,
                                f"{len(mismatch)} 根杆件截面不符"
                                + (f"（另 {len(suspect)} 根随件号 suspect 进 review 队列）" if suspect else "")
                                + f"：{mismatch[:3]}", "bom-section")
    if suspect:
        return ValidationResult(
            rule_id, ValidationStatus.PENDING,
            f"{matched} 核验 / {len(suspect)} 根截面矛盾随件号 suspect 进 review "
            f"队列（bar_id_length_suspect 同源挂接错乱）：{suspect[:3]}",
            "bom-section")
    return ValidationResult(rule_id, ValidationStatus.PASSED,
                            f"{matched} 根杆件截面与 BOM 一致", "bom-section")


def validate_node_fully_solved(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """关键节点三轴坐标已知；cross_file 插值 y 须 y_review=verified。

    P2 语义修正：MLLM/详图页的 2D 节点（view_type=detail，或未声明正交视图）
    本就不该有三轴坐标，缺轴应视为「待跨视图合并」PENDING，而非 FAILED；
    只有已进入 3D 求解链的正交视图节点（front/plan/side/elevation）缺轴才报 FAILED。
    """
    unsolved = []
    pending_2d = []
    derived_pending = []
    for cid, node in _iter_nodes(model):
        p = node.properties
        vt = p.get("view_type")
        if p.get("x") is None or p.get("y") is None or p.get("z") is None:
            # 2D 视图（detail / 无正交视图声明）缺轴 → pending（待跨视图合并）
            if vt in ("detail", None) or vt not in ("front", "plan", "side", "elevation", "section"):
                pending_2d.append(cid)
            else:
                unsolved.append(cid)
            continue
        if p.get("y_origin") == "z_peer_interpolate" and p.get("y_review") != "verified":
            derived_pending.append(cid)
    if unsolved:
        return ValidationResult(rule_id, ValidationStatus.FAILED,
                                f"{len(unsolved)} 个节点缺坐标：{unsolved[:5]}", "node-solved")
    if derived_pending:
        return ValidationResult(
            rule_id, ValidationStatus.PENDING,
            f"{len(derived_pending)} 个节点 y 为 z-peer 插值，待 confirm-derived-y：{derived_pending[:5]}",
            "node-solved",
        )
    if pending_2d:
        return ValidationResult(
            rule_id, ValidationStatus.PENDING,
            f"{len(pending_2d)} 个 2D 视图节点待跨视图合并（缺三轴）：{pending_2d[:5]}",
            "node-solved",
        )
    return ValidationResult(rule_id, ValidationStatus.PASSED,
                            "所有节点三轴坐标已知", "node-solved")


def validate_scan_reviewed(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """P2-5 扫描→终版 3D 闸门：所有扫描候选必须 solve_status=verified。"""
    unreviewed = []
    for cid, comp in model.components.items():
        if comp.kind not in ("tower_bar", "tower_node"):
            continue
        status = comp.properties.get("solve_status")
        if status == "pending_review":
            unreviewed.append(cid)
    if not unreviewed:
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "扫描候选已人工确认（solve_status=verified）", "scan-reviewed")
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"{len(unreviewed)} 个扫描候选仍待人工复核：{unreviewed[:5]}", "scan-reviewed")


def validate_no_duplicate_bar_id(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """杆件编号唯一（物理杆语义）。

    线1 verified delivery（2026-09-03）：与 r_bom_length_match 的
    2026-08-31 物理杆口径修正同纪律——四面镜像（F/B/L/R）、split 细分段、
    sidegen l/r 孪生都是同一物理杆的实例，不计重复；只有**不同物理杆**
    （root stem 不同）共享同一 bar_id 才是重复。此前实例级计数把
    「bar 2 → 25 实例（10 物理杆）」报成 25 组重复——其中 15 组是
    四面镜像伪影。

    P0-3b（2026-09-09）：intake 消歧已裁决形态扩展——组内 primary
    实例集中于唯一 root stem（其余全为 intake 标记的贴线非 primary）
    视为已裁决，与全非 primary 同语义（几何保留、BOM 排除、透明披露）。
    """
    from collections import defaultdict
    from ..project.module_build import _root_stem
    # bar_id -> {root_stem: (view, cid, is_intake_dup)}
    by_id: dict = defaultdict(lambda: defaultdict(list))
    for cid, bar in _iter_bars(model):
        bid = bar.properties.get("bar_id")
        if not bid or str(bid).startswith("UNLABELED"):
            continue
        view = bar.properties.get("view_type", "?")
        # intake 同视图消歧标记：bar_id_dup=True 且非 primary = 该 bar_id
        # 的多余文字（材料表行/远距离错挂），intake 已裁决 primary。
        intake_dup = bool(bar.properties.get("bar_id_dup")
                          and bar.properties.get("bar_id_primary") is False)
        by_id[str(bid)][_root_stem(cid)].append((view, cid, intake_dup))
    # 只保留「不同物理杆共享 bar_id」的组；组内全部实例都是 intake
    # 消歧过的非 primary → 已裁决（透明披露，不进 review 队列）
    dups: dict = {}
    resolved: dict = {}
    for bid, stems in by_id.items():
        if len(stems) <= 1:
            continue
        instances = [inst for lst in stems.values() for inst in lst]
        # P0-3b（2026-09-09）消歧语义补全：intake 消歧（贪心最近文字）对
        # 同 bar_id 多杆贴同文字时已标出唯一 primary stem——组内
        # 「primary 实例集中于同一 root stem、其余全部是 intake_dup」
        # 与 all(intake_dup) 同为已裁决形态（35A1-JC1 实测 17 组全部
        # 是 1 primary stem + 1 dup stem：材料表行/多文字贴线的既知
        # 噪声，几何保留、BOM 计数排除）。仅当 primary 实例本身分布在
        # ≥2 个 root stem（真同号异杆）才进 review 队列。
        primary_stems = {stem for stem, lst in stems.items()
                         if any(not inst[2] for inst in lst)}
        if len(primary_stems) <= 1:
            resolved[bid] = stems
        else:
            dups[bid] = stems
    # P0-3b（2026-09-09）BOM 语义闸（模型内 bom_row 组件可读时）：
    #   * 配件行件号（row_class != member，35A1-JC1 实测 133 连板
    #     -6X115 撞杆号）不属杆件编号语义——披露计数、不进核对队列
    #     （fittings_skipped 同纪律）；
    #   * member 行且物理杆数 ≤ BOM qty：同一件号的合法多位置复用
    #     （110 qty=8：front 主杆 + sidegen 侧读共 4 物理杆）——BOM
    #     本身声明了多件，非编号错误。
    n_fitting = 0
    n_qty_ok = 0
    bom_rows = {}
    for comp in model.components.values():
        if comp.kind == "bom_row":
            p = comp.properties
            bom_rows[str(p.get("bar_id"))] = p
    if bom_rows:
        for bid in list(dups.keys()):
            row = bom_rows.get(bid)
            if row is None:
                continue
            if str(row.get("row_class") or "member") != "member":
                n_fitting += 1
                resolved[bid] = dups.pop(bid)
            else:
                try:
                    qty = int(row.get("qty", 0) or 0)
                except (TypeError, ValueError):
                    qty = 0
                if qty > 0 and len(dups[bid]) <= qty:
                    n_qty_ok += 1
                    resolved[bid] = dups.pop(bid)
    n_resolved = len(resolved)
    extra = ""
    if n_fitting or n_qty_ok:
        extra = (f"；{n_fitting} 组配件行撞号、{n_qty_ok} 组合法多位置"
                 f"（物理杆数 ≤ BOM qty）已按 BOM 语义裁决")
    if not dups:
        msg = ("杆件编号物理杆级唯一（镜像/细分/孪生合并；"
               + (f"{n_resolved} 组同视图多文字已由 intake 消歧标记非 primary，"
                  "BOM 计数排除、几何保留" if n_resolved else "无重复")
               + extra + ")")
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                msg, "no-dup-bar-id")
    df = model.components.get("drawing_file")
    is_cross_file = bool(df and df.properties.get("view_mode") == "cross_file_multi_view")
    detail = ", ".join(
        f"{bid}×{len(stems)}杆" for bid, stems in sorted(dups.items())[:6])
    if is_cross_file:
        return ValidationResult(
            rule_id, ValidationStatus.PENDING,
            f"cross_file 合并后 {len(dups)} 组件号跨物理杆复用，待人工核对"
            f"（同杆镜像/细分已合并；{n_resolved} 组同视图多文字已由 intake 消歧）：{detail}",
            "no-dup-bar-id",
        )
    return ValidationResult(rule_id, ValidationStatus.FAILED,
                            f"{len(dups)} 组物理杆级重复编号：{detail}",
                            "no-dup-bar-id")


def validate_gusset_plate(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """节点板大样：局部轮廓存在；未锚定全局坐标时为 pending。"""
    plate_id = rule_id.removeprefix("r_gusset_")
    cid = f"gusset_{plate_id}"
    comp = model.components.get(cid)
    if comp is None:
        comp = model.components.get(plate_id)
    if comp is None:
        rule = model.rules.get(rule_id)
        if rule and rule.applies_to:
            comp = model.components.get(rule.applies_to[0])
    if comp is None:
        return ValidationResult(rule_id, ValidationStatus.FAILED,
                                f"节点板组件 {cid} 不存在", "gusset-plate")
    poly = comp.properties.get("polygon_local") or []
    if len(poly) < 3:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "节点板轮廓不足 3 顶点", "gusset-plate")
    if comp.properties.get("global_coords") == "pending_anchor":
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "局部轮廓已抽取，待锚定全局坐标", "gusset-plate")
    if comp.properties.get("solve_status") == "verified":
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "节点板已锚定并输出全局坐标", "gusset-plate")
    return ValidationResult(rule_id, ValidationStatus.PASSED,
                            f"节点板局部轮廓 {len(poly)} 顶点", "gusset-plate")


def validate_bolt_group_rule(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """螺栓群验算：复用 connection.bolt_verify，与 tower_detail 注入规则一致。"""
    from ..connection.bolt_verify import BoltGroup, BoltSpec, bolt_group_detail_key, verify_bolt_group

    rule = model.rules.get(rule_id)
    comp = None
    cid: Optional[str] = None
    if rule and rule.applies_to:
        cid = rule.applies_to[0]
        comp = model.components.get(cid)
    if comp is None:
        gid = rule_id.removeprefix("r_bolt_group_")
        for k, c in model.components.items():
            if c.kind != "bolt_group":
                continue
            if k == gid or k == f"bolt_group_{gid}" or k.endswith(f"__{gid}") or gid in k:
                comp = c
                cid = k
                break
    if comp is None:
        return ValidationResult(rule_id, ValidationStatus.FAILED,
                                f"螺栓群组件 {rule_id.removeprefix('r_bolt_group_')} 不存在", "bolt-group")
    gid = (
        cid.split("__bolt_group_", 1)[-1]
        if cid and "__bolt_group_" in cid
        else (cid or "").removeprefix("bolt_group_")
    )
    holes_raw = comp.properties.get("holes") or []
    holes = [(float(h[0]), float(h[1])) for h in holes_raw if h]
    spec = BoltSpec(
        count=int(comp.properties.get("count") or len(holes) or 1),
        diameter_mm=float(comp.properties.get("diameter_mm") or 16),
        length_mm=float(comp.properties.get("length_mm") or 40),
    )
    outline_raw = _gusset_outline_for_bolt(model, gid, component_id=cid)
    outline = outline_raw or None
    group = BoltGroup(group_id=gid, spec=spec, holes=holes,
                      plate_outline=outline or None)
    result = verify_bolt_group(group)
    st = ValidationStatus(result["status"])
    msg = "; ".join(result["issues"]) if result["issues"] else "passed"
    return ValidationResult(rule_id, st, msg, "bolt-group")


def connection_validators_for_model(model: EngineeringModel) -> dict:
    """为模型中的 r_gusset_* / r_bolt_group_* 动态规则注册验证器。"""
    out: dict = {}
    for rid in model.rules:
        if rid.startswith("r_gusset_"):
            out[rid] = validate_gusset_plate
        elif rid.startswith("r_bolt_group_"):
            out[rid] = validate_bolt_group_rule
    return out


def validate_cross_file_3d_partial(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """跨文件合并：至少部分节点解出三轴坐标（国网 front+plan 分册场景）。"""
    df = model.components.get("drawing_file")
    if df is None or df.properties.get("view_mode") != "cross_file_multi_view":
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "非 cross_file 合并模型，跳过", "cross-file-3d")
    nodes = [c for c in model.components.values() if c.kind == "tower_node"]
    solved = [c for c in nodes if c.properties.get("solve_status") == "solved"
              and c.properties.get("x") is not None and c.properties.get("y") is not None
              and c.properties.get("z") is not None]
    if not nodes:
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "无 tower_node 可核验", "cross-file-3d")
    if not solved:
        return ValidationResult(rule_id, ValidationStatus.FAILED,
                                "cross_file 合并后 nodes_solved=0", "cross-file-3d")
    return ValidationResult(
        rule_id, ValidationStatus.PASSED,
        f"cross_file 部分 3D 解算 {len(solved)}/{len(nodes)} 节点",
        "cross-file-3d",
    )


def validate_cross_file_y_derived(model: EngineeringModel, rule_id: str) -> Optional[ValidationResult]:
    """cross_file z-peer 插值 y：存在则 pending，人工 confirm 后 passed。"""
    df = model.components.get("drawing_file")
    if df is None or df.properties.get("view_mode") != "cross_file_multi_view":
        return ValidationResult(rule_id, ValidationStatus.PENDING,
                                "非 cross_file 模型，跳过", "cross-file-y-derived")
    pending = [
        cid for cid, comp in model.components.items()
        if comp.kind == "tower_node"
        and comp.properties.get("y_origin") == "z_peer_interpolate"
        and comp.properties.get("y_review") != "verified"
    ]
    if not pending:
        has_any = any(
            c.properties.get("y_origin") == "z_peer_interpolate"
            for c in model.components.values() if c.kind == "tower_node"
        )
        if has_any:
            return ValidationResult(rule_id, ValidationStatus.PASSED,
                                    "插值 y 已全部人工复核", "cross-file-y-derived")
        return ValidationResult(rule_id, ValidationStatus.PASSED,
                                "无 z-peer 插值 y 节点", "cross-file-y-derived")
    return ValidationResult(
        rule_id, ValidationStatus.PENDING,
        f"{len(pending)} 个节点 y 为插值 derived，待 confirm-derived-y",
        "cross-file-y-derived",
    )


# 规则 ID -> 验证器
tower_validators = {
    "r_topology_closed": validate_topology_closed,
    "r_bom_length_match": validate_bom_length_match,
    "r_bom_section_match": validate_bom_section_match,
    "r_node_fully_solved": validate_node_fully_solved,
    "r_no_duplicate_bar_id": validate_no_duplicate_bar_id,
    "r_scan_reviewed": validate_scan_reviewed,
    "r_cross_file_3d_partial": validate_cross_file_3d_partial,
    "r_cross_file_y_derived": validate_cross_file_y_derived,
}


TOWER_RULE_DEFS = [
    {
        "id": "r_topology_closed",
        "name": "铁塔杆件拓扑闭合",
        "description": "每根杆件两端节点必须存在（from_node/to_node 指向 tower_node）",
    },
    {
        "id": "r_bom_length_match",
        "name": "杆件长度与 BOM 核验",
        "description": "杆件 3D 长度 vs BOM 长度，偏差 ≤ 3%",
    },
    {
        "id": "r_bom_section_match",
        "name": "杆件截面与 BOM 核验",
        "description": "杆件截面规格归一化后与 BOM 相等",
    },
    {
        "id": "r_node_fully_solved",
        "name": "节点三轴坐标齐备",
        "description": "关键节点 x/y/z 三轴坐标已知，无 placeholder",
    },
    {
        "id": "r_no_duplicate_bar_id",
        "name": "杆件编号唯一",
        "description": "同一视图内杆件编号不重复",
    },
    {
        "id": "r_scan_reviewed",
        "name": "扫描图人工复核闸门",
        "description": "扫描图候选杆件/节点必须 solve_status=verified 才可进终版 3D",
    },
    {
        "id": "r_cross_file_3d_partial",
        "name": "跨文件 3D 部分解算",
        "description": "cross_file 合并后至少部分节点解出 x/y/z（front+plan 分册）",
    },
    {
        "id": "r_cross_file_y_derived",
        "name": "跨文件插值 Y 复核",
        "description": "z-peer 插值 y 须人工 confirm-derived-y 后才可终版 GLB",
    },
]


def inject_tower_rules(model: EngineeringModel) -> EngineeringModel:
    """把铁塔验证规则写入模型（applies_to 指向实际构件）。

    基础五条始终注入；r_scan_reviewed 只在存在扫描候选
    （solve_status=pending_review）时注入。
    """
    from ..model import Rule

    bar_ids = [cid for cid, c in model.components.items() if c.kind == "tower_bar"]
    node_ids = [cid for cid, c in model.components.items() if c.kind == "tower_node"]
    has_scan = any(
        c.properties.get("solve_status") == "pending_review"
        for c in model.components.values() if c.kind in ("tower_bar", "tower_node")
    )
    df = model.components.get("drawing_file")
    is_cross_file = bool(df and df.properties.get("view_mode") == "cross_file_multi_view")
    has_derived_y = any(
        c.properties.get("y_origin") == "z_peer_interpolate"
        for c in model.components.values() if c.kind == "tower_node"
    )

    specs = list(TOWER_RULE_DEFS)
    if not has_scan:
        specs = [sp for sp in specs if sp["id"] != "r_scan_reviewed"]
    if not is_cross_file:
        specs = [sp for sp in specs if sp["id"] not in ("r_cross_file_3d_partial", "r_cross_file_y_derived")]
    elif not has_derived_y:
        specs = [sp for sp in specs if sp["id"] != "r_cross_file_y_derived"]

    for spec in specs:
        rid = spec["id"]
        if rid in model.rules:
            continue
        if rid in ("r_topology_closed", "r_bom_length_match",
                   "r_bom_section_match", "r_no_duplicate_bar_id"):
            applies_to = bar_ids
        elif rid == "r_scan_reviewed":
            applies_to = bar_ids + node_ids
        else:
            applies_to = node_ids
        model.add_rule(Rule(
            id=rid,
            name=spec["name"],
            description=spec["description"],
            applies_to=applies_to,
        ))
    inject_connection_rules(model)
    return model


def inject_connection_rules(model: EngineeringModel) -> EngineeringModel:
    """Gap 2：为节点板/螺栓群注入 Harness 规则（与 tower_detail 主链衔接）。"""
    from ..model import Rule, ValidationStatus

    for cid, comp in model.components.items():
        if comp.kind == "gusset_plate":
            plate_id = cid.removeprefix("gusset_")
            rid = f"r_gusset_{plate_id}"
            if rid in model.rules:
                continue
            poly = comp.properties.get("polygon_local") or []
            if comp.properties.get("solve_status") == "verified":
                st, msg = ValidationStatus.PASSED, "节点板已锚定"
            elif comp.properties.get("global_coords") == "pending_anchor" and len(poly) >= 3:
                st, msg = ValidationStatus.PENDING, "局部轮廓已抽取，待锚定全局坐标"
            elif len(poly) >= 3:
                st, msg = ValidationStatus.PENDING, "节点板待复核"
            else:
                st, msg = ValidationStatus.PENDING, "节点板轮廓不完整"
            model.add_rule(Rule(
                id=rid,
                name=f"节点板验算 {plate_id}",
                description="大样节点板轮廓与锚定状态",
                applies_to=[cid],
                status=st,
                message=msg,
            ))

    from ..connection.bolt_verify import BoltGroup, BoltSpec, inject_bolt_verification_rule, verify_bolt_group

    for cid, comp in model.components.items():
        if comp.kind != "bolt_group":
            continue
        gid = cid.removeprefix("bolt_group_")
        rid = f"r_bolt_group_{gid}"
        if rid in model.rules:
            continue
        holes_raw = comp.properties.get("holes") or []
        holes = [(float(h[0]), float(h[1])) for h in holes_raw if h]
        spec = BoltSpec(
            count=int(comp.properties.get("count") or len(holes) or 1),
            diameter_mm=float(comp.properties.get("diameter_mm") or 16),
            length_mm=float(comp.properties.get("length_mm") or 40),
        )
        outline = _gusset_outline_for_bolt(model, gid, component_id=cid)
        group = BoltGroup(group_id=gid, spec=spec, holes=holes, plate_outline=outline)
        result = verify_bolt_group(group)
        inject_bolt_verification_rule(model, group, result, component_id=cid)
    return model


def _gusset_outline_for_bolt(
    model: EngineeringModel,
    group_id: str,
    *,
    component_id: Optional[str] = None,
):
    """按 bolt group id 找同大样节点板轮廓（兼容 cross_file 前缀）。"""
    from ..connection.bolt_verify import bolt_group_detail_key

    plate_key = bolt_group_detail_key(group_id)
    for gcid, gcomp in model.components.items():
        if gcomp.kind != "gusset_plate":
            continue
        detail_id = str(gcomp.properties.get("detail_id") or "")
        pid = gcid.removeprefix("gusset_")
        if (
            detail_id == plate_key
            or pid == plate_key
            or pid.endswith(f"__gusset_{plate_key}")
            or gcid.endswith(f"__gusset_{plate_key}")
        ):
            raw = gcomp.properties.get("polygon_local") or []
            return [(float(p[0]), float(p[1])) for p in raw if p]
    if component_id:
        from ..connection.bolt_mesh import _gusset_for_bolt

        gusset_cid = _gusset_for_bolt(model, component_id)
        if gusset_cid:
            raw = model.components[gusset_cid].properties.get("polygon_local") or []
            return [(float(p[0]), float(p[1])) for p in raw if p]
    return None
