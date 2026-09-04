#!/usr/bin/env python3
"""P1 离线实验：S4 共线断裂斜材拼接（collinear stitching）——只诊断，不改生产代码。

背景（2026-08-31 诊断）
----------------------
模型杆件**碎片化**是召回率头号瓶颈：GT 杆件长度中位 2005mm（leg 2506 /
diagonal 3286），模型纯 DXF 杆件中位仅 888mm，长度比 2.26×。一根 GT 杆被切成
2~3 段后，端点误差天然达 1000mm 量级，500mm 容差无论如何调都过不去——这也解释
了为什么「端点吸附 20~80mm 缝隙」（任务 2）治不了本症。

本脚本在**模型后处理阶段**把共线断裂的碎片拼回整杆，用实测数据回答：
    「拼接到底能把 A2 召回率推高多少？」

与生产代码的关系
----------------
本脚本**不修改** traceability/solve/tower_geometry.py 等生产模块（该模块正被
Phase 2.3 受控吸附工作并行修改，避免叠加冲突）。验证出收益后再决定如何集成。

拼接判据
--------
    1. 同一 face（不同立面不拼）
    2. 端点间隙 <= --gap（默认 80mm）
    3. 无向夹角 <= --ang（默认 5°）
    4. 跳过横隔（diaphragm=True）与横担（role=CROSS）
    5. 合并链：A-B、B-C 可拼则 A-B-C 一起合（并查集）

合成杆语义
----------
    * 删除参与合并的碎片，只保留合成杆（GT 一根即一根，模型也应为根）
    * geometry_class = "recognized"（拼接自两根 recognized 杆，属识别产物合并）
    * geometry_origin = "collinear_stitch"（透明化，可在口径审计中单列）
    * 保留 root_bar_id 证据链：拼接来源全部记录到 stitched_from

用法
----
    python3 scripts/experiment_collinear_stitch.py <model.json> <gt.json> \
        [--gap 80] [--ang 5] [--min-merged-len 600] [--out out/stitched.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from traceability.eval.metrics import (  # noqa: E402
    DEFAULT_TOLS, eval_a2_dual_caliber,
)

Vec3 = Tuple[float, float, float]


# --------------------------------------------------------------------------- #
# 几何工具
# --------------------------------------------------------------------------- #

def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(v: Vec3) -> float:
    return math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)


def _unit(v: Vec3) -> Optional[Vec3]:
    n = _norm(v)
    if n < 1e-9:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def _dist(a: Vec3, b: Vec3) -> float:
    return _norm(_sub(a, b))


def _angle_deg(u: Vec3, v: Vec3) -> float:
    """无向夹角（度）。"""
    d = u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
    d = max(-1.0, min(1.0, abs(d)))
    return math.degrees(math.acos(d))


# --------------------------------------------------------------------------- #
# 拼接核心
# --------------------------------------------------------------------------- #

class _DSU:
    def __init__(self):
        self.p: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def stitch_model(
    model: Dict[str, Any],
    *,
    gap_mm: float = 80.0,
    ang_deg: float = 5.0,
    min_merged_len: float = 600.0,
    mode: str = "greedy",
    max_merged_len: float = 4500.0,
    max_segments: int = 3,
    target_len: float = 2018.0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """对 model.json 做共线拼接，返回 (新 model, 统计报告)。

    mode="greedy"（默认，Round 1 起）：**贪心成对合并**。
        每轮在所有候选对中选「合成后长度最接近 GT 中位」的一对合并，合并后
        新杆继续参与下一轮。天然避免过合并——杆越长越难再找到合格配对，
        且受 max_merged_len / max_segments 双重约束。

    mode="union_find"（Round 0 版，保留作对照）：并查集全连通合并。
        实测会把整条主腿的 51 个节间并成一根 17m 超长杆（GT 最长仅 7077mm），
        导致 A2-pure 56→26、A2-full 188→155。**已证实不可用，仅作回归对照。**
    """
    comps = model.get("components", {})
    nodes = {k: v for k, v in comps.items() if v.get("kind") == "tower_node"}
    bars = {k: v for k, v in comps.items() if v.get("kind") == "tower_bar"}

    def pos(nid: str) -> Optional[Vec3]:
        n = nodes.get(nid)
        if not n:
            return None
        p = n.get("properties", {})
        if p.get("x") is None or p.get("y") is None or p.get("z") is None:
            return None
        return (float(p["x"]), float(p["y"]), float(p["z"]))

    # 候选杆：跳过横隔 / 横担 / 无法定位端点的杆
    cand: Dict[str, Tuple[Vec3, Vec3, dict]] = {}
    skipped = Counter()
    for bid, b in bars.items():
        p = b.get("properties", {})
        if p.get("diaphragm"):
            skipped["diaphragm"] += 1
            continue
        if str(p.get("role") or "").upper() == "CROSS":
            skipped["crossarm"] += 1
            continue
        a, c = pos(p.get("from_node")), pos(p.get("to_node"))
        if a is None or c is None:
            skipped["no_endpoint"] += 1
            continue
        if _dist(a, c) < 1e-6:
            skipped["degenerate"] += 1
            continue
        cand[bid] = (a, c, p)

    # 按 face 分桶，只在同面内比较（跨面共线是镜像假象）
    by_face: Dict[str, List[str]] = defaultdict(list)
    for bid, (_, _, p) in cand.items():
        by_face[str(p.get("face") or p.get("generated_face") or "?")].append(bid)

    # 合成杆的语义类别跟踪（防止镜像面杆件被拼接洗白，见下方 geometry_class 继承）
    _class_of: Dict[str, str] = {
        bid: str(v[2].get("geometry_class") or "") for bid, v in cand.items()
    }

    def _pair_ok(x: Tuple[Vec3, Vec3], y: Tuple[Vec3, Vec3]) -> bool:
        """两端是否共线且端点接近（构成断裂点）。"""
        ux, uy = _unit(_sub(x[1], x[0])), _unit(_sub(y[1], y[0]))
        if ux is None or uy is None:
            return False
        if _angle_deg(ux, uy) > ang_deg:
            return False
        return min(_dist(x[0], y[0]), _dist(x[0], y[1]),
                   _dist(x[1], y[0]), _dist(x[1], y[1])) <= gap_mm

    n_pairs = 0
    groups: Dict[str, List[str]] = defaultdict(list)

    if mode == "greedy":
        # ---- 贪心成对合并：增量维护候选对，每轮取「最贴合 GT 语义」的一对 ----
        # 活跃杆集合：id -> (p1, p2, n_segments)
        active: Dict[str, Tuple[Vec3, Vec3, int]] = {
            bid: (v[0], v[1], 1) for bid, v in cand.items()
        }
        face_of: Dict[str, str] = {}
        for fid, ids in by_face.items():
            for bid in ids:
                face_of[bid] = fid

        # 初始候选对
        pairs: List[Tuple[float, str, str]] = []  # (|合成长度-target|, i, j)
        for _fid, ids in by_face.items():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    if _pair_ok(cand[ids[i]], cand[ids[j]]):
                        n_pairs += 1
                        L = _dist(cand[ids[i]][0], cand[ids[j]][0])
                        for pa in cand[ids[i]][:2]:
                            for pb in cand[ids[j]][:2]:
                                L = max(L, _dist(pa, pb))
                        if L <= max_merged_len:
                            pairs.append((abs(L - target_len), ids[i], ids[j]))
        pairs.sort(key=lambda t: t[0])

        consumed: set = set()
        merged_chains: Dict[str, List[str]] = {}
        new_id_seq = 0
        while pairs:
            _score, bi, bj = pairs.pop(0)
            if bi in consumed or bj in consumed:
                continue
            ai_, aj_ = active.get(bi), active.get(bj)
            if ai_ is None or aj_ is None:
                continue
            if ai_[2] + aj_[2] > max_segments:
                continue
            # 合成端点：主轴投影极值
            axis = _unit(_sub(ai_[1], ai_[0])) or _unit(_sub(aj_[1], aj_[0]))
            if axis is None:
                continue
            pts = [ai_[0], ai_[1], aj_[0], aj_[1]]
            proj = sorted(((sum(p[k] * axis[k] for k in range(3)), p) for p in pts),
                          key=lambda t: t[0])
            p_s, p_e = proj[0][1], proj[-1][1]
            L = _dist(p_s, p_e)
            if L < min_merged_len or L > max_merged_len:
                continue
            new_id_seq += 1
            nid = f"stitch__g{new_id_seq}"
            cb = _class_of.get(bi, "")
            cj2 = _class_of.get(bj, "")
            _class_of[nid] = ("recognized"
                              if cb == "recognized" and cj2 == "recognized"
                              else (cb or cj2))
            active[nid] = (p_s, p_e, ai_[2] + aj_[2])
            face_of[nid] = face_of.get(bi, "?")
            merged_chains[nid] = merged_chains.get(bi, [bi]) + merged_chains.get(bj, [bj])
            consumed.add(bi)
            consumed.add(bj)
            # 增量：新杆与所有同面活跃杆重新配对
            fn = face_of[nid]
            for other, ov in active.items():
                if other == nid or other in consumed:
                    continue
                if face_of.get(other) != fn:
                    continue
                if not _pair_ok((p_s, p_e), (ov[0], ov[1])):
                    continue
                if active[nid][2] + ov[2] > max_segments:
                    continue
                L2 = max(_dist(p_s, ov[0]), _dist(p_s, ov[1]),
                         _dist(p_e, ov[0]), _dist(p_e, ov[1]))
                if L2 > max_merged_len:
                    continue
                pairs.append((abs(L2 - target_len), nid, other))
            pairs.sort(key=lambda t: t[0])

        # 组装合并组
        for nid, chain in merged_chains.items():
            groups[nid] = chain
        # 未参与合并的杆保持独立
        for bid in active:
            if bid not in consumed and not bid.startswith("stitch__"):
                groups[bid] = [bid]

    else:
        # ---- 并查集全连通（Round 0 版，仅作对照，已知会过合并）----
        dsu = _DSU()
        for _face, ids in by_face.items():
            for i in range(len(ids)):
                ai, (a1, a2, _) = ids[i], cand[ids[i]]
                ua = _unit(_sub(a2, a1))
                if ua is None:
                    continue
                for j in range(i + 1, len(ids)):
                    bj, (b1, b2, _) = ids[j], cand[ids[j]]
                    ub = _unit(_sub(b2, b1))
                    if ub is None:
                        continue
                    if _angle_deg(ua, ub) > ang_deg:
                        continue
                    if min(_dist(a1, b1), _dist(a1, b2),
                           _dist(a2, b1), _dist(a2, b2)) <= gap_mm:
                        dsu.union(ai, bj)
                        n_pairs += 1
        for bid in cand:
            groups[dsu.find(bid)].append(bid)

    new_comps = {k: v for k, v in comps.items()}
    stats = {
        "n_bars_in": len(bars),
        "n_candidates": len(cand),
        "skipped": dict(skipped),
        "collinear_pairs": n_pairs,
        "merged_groups": 0,
        "bars_removed": 0,
        "bars_created": 0,
        "len_before": [],
        "len_after": [],
        "by_role": Counter(),
    }

    # 统一待写入列表：[(new_id, p_start, p_end, members)]
    if mode == "greedy":
        pending = [(nid, active[nid][0], active[nid][1], chain)
                   for nid, chain in merged_chains.items()]
    else:
        pending = []
        for _root, members in groups.items():
            if len(members) < 2:
                continue
            base = max(members, key=lambda b: _dist(*cand[b][:2]))
            b1, b2, _ = cand[base]
            axis = _unit(_sub(b2, b1))
            if axis is None:
                continue
            pts: List[Vec3] = []
            for m in members:
                pts.extend([cand[m][0], cand[m][1]])
            proj = [(sum(p[k] * axis[k] for k in range(3)), p) for p in pts]
            proj.sort(key=lambda t: t[0])
            pending.append((f"stitch__{members[0]}", proj[0][1], proj[-1][1], members))

    for new_id, p_start, p_end, members in pending:
        merged_len = _dist(p_start, p_end)
        if merged_len < min_merged_len:
            stats["skipped"]["merged_too_short"] = \
                stats["skipped"].get("merged_too_short", 0) + 1
            continue
        # 段数上限（greedy 已在配对阶段约束，此处兜底；union_find 模式的护栏）
        if len(members) > max_segments and mode != "union_find":
            stats["skipped"]["too_many_segments"] = \
                stats["skipped"].get("too_many_segments", 0) + 1
            continue

        # 建两个新端点节点
        nid_s = f"{new_id}__S"
        nid_e = f"{new_id}__E"
        for nid, pt in ((nid_s, p_start), (nid_e, p_end)):
            new_comps[nid] = {
                "id": nid, "name": nid, "kind": "tower_node", "source": None,
                "properties": {
                    "x": pt[0], "y": pt[1], "z": pt[2],
                    "solve_status": "solved",
                    "geometry_origin": "collinear_stitch",
                },
                "tags": [],
            }

        src_props = cand[members[0]][2]
        new_props = dict(src_props)
        # 语义继承（2026-08-31 修正）：合成杆的 geometry_class 必须继承源杆，
        # 不能硬编码 recognized——否则镜像面（b/l/r，非 recognized）的杆件
        # 会被拼接「洗白」成直接识别杆，污染 recognition 口径。
        # 规则：全部源杆均为 recognized 才标 recognized；否则继承首个源杆的类别。
        src_classes = [
            _class_of.get(m) or str(cand[m][2].get("geometry_class") or "")
            for m in members if m in cand or m in _class_of
        ]
        if src_classes and all(c == "recognized" for c in src_classes):
            inherit_cls = "recognized"
        else:
            inherit_cls = next((c for c in src_classes if c),
                               str(src_props.get("geometry_class") or ""))
        new_props["geometry_class"] = inherit_cls
        new_props.update({
            "from_node": nid_s,
            "to_node": nid_e,
            # P3-7（2026-09-04）：此处曾硬编码 "geometry_class": "recognized"，
            # 把上一行刚算出的防洗白守卫 inherit_cls 无条件覆盖——镜像面
            # （b/l/r）源杆拼接后被洗白成直读杆，2026-08-31 的修正意图
            # 从未生效。删除该键，让 inherit_cls 生效。
            "geometry_origin": "collinear_stitch",
            "stitched_from": list(members),
            "stitched_n_segments": len(members),
            "length": merged_len,
        })
        new_comps[new_id] = {
            "id": new_id,
            "name": f"stitch({len(members)})_{src_props.get('bar_id', members[0])}",
            "kind": "tower_bar",
            "source": bars[members[0]].get("source"),
            "properties": new_props,
            "tags": [],
        }

        for m in members:
            if m in cand:
                stats["len_before"].append(_dist(*cand[m][:2]))
            new_comps.pop(m, None)
        stats["len_after"].append(merged_len)
        stats["merged_groups"] += 1
        stats["bars_removed"] += len(members)
        stats["bars_created"] += 1
        stats["by_role"][str(src_props.get("role") or "?")] += 1

    new_model = dict(model)
    new_model["components"] = new_comps
    stats["n_bars_out"] = sum(
        1 for v in new_comps.values() if v.get("kind") == "tower_bar")

    def _med(xs: List[float]) -> float:
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else 0.0

    stats["len_before_median"] = round(_med(stats["len_before"]), 1)
    stats["len_after_median"] = round(_med(stats["len_after"]), 1)
    stats["by_role"] = dict(stats["by_role"])
    del stats["len_before"], stats["len_after"]
    return new_model, stats


# --------------------------------------------------------------------------- #

def _fmt_sweep(title: str, block: dict, extra: str = "") -> None:
    print(f"\n{title}")
    print(f"{'tol(mm)':>8} {'TP':>5} {'FP':>5} {'FN':>5} {'Precision':>10} {'Recall':>10}{extra}")
    for s in block["sweep"]:
        print(f"{s['tol']:>8.0f} {s['tp']:>5} {s['fp']:>5} {s['fn']:>5} "
              f"{s['precision']:>10.1%} {s['recall']:>10.1%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("gt")
    ap.add_argument("--gap", type=float, default=80.0)
    ap.add_argument("--ang", type=float, default=5.0)
    ap.add_argument("--min-merged-len", type=float, default=600.0)
    ap.add_argument("--max-merged-len", type=float, default=4500.0,
                    help="合成杆长度上限（GT 最长 7077mm，p75 3607mm）")
    ap.add_argument("--max-segments", type=int, default=3,
                    help="单根合成杆最多由几段碎片拼成")
    ap.add_argument("--target-len", type=float, default=2018.0,
                    help="贪心优先级：合成长度最接近该值者优先（默认 GT 中位）")
    ap.add_argument("--mode", choices=["greedy", "union_find"], default="greedy")
    ap.add_argument("--view", default="front")
    ap.add_argument("--out", default=None, help="写出拼接后的 model.json")
    args = ap.parse_args()

    model = json.loads(Path(args.model).read_text(encoding="utf-8"))
    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))

    before = eval_a2_dual_caliber(gt, model, view=args.view, tols=DEFAULT_TOLS)
    stitched, stats = stitch_model(
        model, gap_mm=args.gap, ang_deg=args.ang,
        min_merged_len=args.min_merged_len, mode=args.mode,
        max_merged_len=args.max_merged_len, max_segments=args.max_segments,
        target_len=args.target_len)
    after = eval_a2_dual_caliber(gt, stitched, view=args.view, tols=DEFAULT_TOLS)

    print("=== S4 共线拼接实验 ===")
    print(f"模式 {args.mode}；判据：同 face、端点间隙 <= {args.gap}mm、"
          f"无向夹角 <= {args.ang}°、合成长度 ∈ [{args.min_merged_len:.0f}, "
          f"{args.max_merged_len:.0f}]mm、最多 {args.max_segments} 段")
    print(f"候选杆 {stats['n_candidates']}（跳过 {stats['skipped']}）")
    print(f"共线对 {stats['collinear_pairs']} → 合并组 {stats['merged_groups']}")
    print(f"杆件 {stats['n_bars_in']} → {stats['n_bars_out']} "
          f"（删 {stats['bars_removed']} / 建 {stats['bars_created']}）")
    print(f"合并前碎片长度中位 {stats['len_before_median']}mm → "
          f"合成后 {stats['len_after_median']}mm")
    print(f"按角色合并：{stats['by_role']}")

    print("\n=== A2-pure（纯 DXF 主口径）===")
    _fmt_sweep("拼接前", before["pure_dxf"])
    _fmt_sweep("拼接后", after["pure_dxf"])
    for tol in (200.0, 500.0):
        b = next(s for s in before["pure_dxf"]["sweep"] if s["tol"] == tol)
        a = next(s for s in after["pure_dxf"]["sweep"] if s["tol"] == tol)
        print(f"  tol={tol:.0f}: TP {b['tp']} → {a['tp']}  "
              f"(+{a['tp']-b['tp']})  召回 {b['recall']:.1%} → {a['recall']:.1%}")

    print("\n=== A2-full（physical 增强口径）===")
    for tol in (200.0, 500.0):
        b = next(s for s in before["full"]["sweep"] if s["tol"] == tol)
        a = next(s for s in after["full"]["sweep"] if s["tol"] == tol)
        print(f"  tol={tol:.0f}: TP {b['tp']} → {a['tp']}  "
              f"(+{a['tp']-b['tp']})  召回 {b['recall']:.1%} → {a['recall']:.1%}"
              f"  精确率 {b['precision']:.1%} → {a['precision']:.1%}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(stitched, ensure_ascii=False), encoding="utf-8")
        print(f"\n拼接后模型已写出: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
