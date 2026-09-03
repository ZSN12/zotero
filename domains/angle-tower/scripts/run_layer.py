#!/usr/bin/env python3
"""六层契约的独立可跑入口（run_layer）。

开源基座对标（2026-09-03）：SKILL.md 第 1 节的六层契约
（drawing → hypothesis → rebuild → semantic-ir → validation-gate →
complete-tower）此前只有文档，没有逐层可跑/可审计的命令。本脚本让
每一层都成为独立入口：

  用法 A（工作区模式，先跑管线再审计）：
    python3 run_layer.py <layer> --workspace <dir>

  用法 B（审计模式，只检查已有交付产物，不重跑）：
    python3 run_layer.py <layer> --out-dir <dir>

  layer ∈ 1..6 或名称：
    1 drawing          L1 图纸提取：recognized 几何 + SourceRef + 证据观测
    2 hypothesis       L2 结构假设：四态状态机 + 观测普查
    3 rebuild          L3 参数化补全：geometry_origin 归因 + 节点求解
    4 semantic-ir      L4 语义工程模型：schema + BOM 交叉核验
    5 validation-gate  L5 规则裁决：Rule.status + pending/failed 清单
    6 complete-tower   L6 完整交付：GLB + version.json 指纹链

设计原则（诚实性契约）：
  * 审计模式是主形态——层入口审计的是**canonical 产物**
    （deliver_project / run_tower 的输出），绝不重演编排逻辑，
    两条路径不会分叉。
  * 工作区模式跑的也是 canonical 引擎入口：单 DXF → run_tower；
    多册 + overlay → deliver_project + write_version_manifest。
    与 scripts/run_<tower>_full.py 同一套引擎函数。
  * 每层审计结果落盘 out/layer{N}_{name}_audit.json，可复查。

退出码：0 审计通过；1 契约违例（逐条列出）；2 前置缺失
（无产物且无法运行）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

CALIBER_ORIGINS_REQUIRED = ("recognized", "reconstructed", "derived_parametric")
HYPOTHESIS_STATUSES = {"proposed", "accepted", "rejected", "superseded"}
RULE_STATUSES = {"passed", "failed", "pending", "not_applicable", "review_exempted"}
LAYER_NAMES = {
    1: "drawing", 2: "hypothesis", 3: "rebuild",
    4: "semantic-ir", 5: "validation-gate", 6: "complete-tower",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _model_bars(model: dict):
    return [c for c in (model.get("components") or {}).values()
            if c.get("kind") == "tower_bar"]


def _model_nodes(model: dict):
    return [c for c in (model.get("components") or {}).values()
            if c.get("kind") == "tower_node"]


def _evidence_census(model: dict) -> dict:
    df = (model.get("components") or {}).get("drawing_file") or {}
    ev = ((df.get("properties") or {}).get("evidence_layer")) or {}
    return ev.get("observations") or {}


# ---------------------------------------------------------------- 运行步骤

def _run_pipeline(workspace: Path, out_dir: Path) -> str:
    """工作区模式：跑 canonical 引擎入口，产物落 out_dir。返回入口名。"""
    dxf_dir = workspace / "dxf"
    dxfs = sorted(dxf_dir.glob("*.dxf")) + sorted(dxf_dir.glob("*.dwg"))
    if not dxfs:
        raise RuntimeError(f"工作区无 DXF/DWG：{dxf_dir}")
    overlay = workspace / "overlay.json"
    bom = workspace / "bom" / "bom.csv"

    if len(dxfs) == 1:
        # 单册塔：run_tower 是 canonical 入口（deliver_project 的 cross-file
        # 合并需要 overlay 多册声明，单册会 NO_3D_MODEL）。
        from traceability.harness.tower_harness import run_tower
        kwargs = {}
        if bom.exists():
            kwargs["bom_path"] = str(bom)
        run_tower(dxfs[0], out_dir, merge=True, **kwargs)
        entry = "run_tower"
    else:
        if not overlay.exists():
            raise RuntimeError(
                f"多册工作区必须提供 {overlay}（view_regions/cross_file_views）")
        from traceability.project.delivery import deliver_project
        deliver_project(
            dxf_dir, out_dir,
            project_id=workspace.name,
            layer_map_path=str(overlay),
            bom_path=str(bom) if bom.exists() else None,
        )
        entry = "deliver_project"

    # L6 指纹链：version.json（run_<tower>_full.py 的 postprocess 同款调用）
    from traceability.project.versioning import write_version_manifest
    write_version_manifest(
        out_dir, REPO,
        overlay if overlay.exists() else None)
    return entry


def _ensure_artifacts(args, out_dir: Path) -> tuple[Path, str]:
    """确保 out_dir 有 model.json。返回 (model_path, 来源说明)。"""
    model_path = out_dir / "model.json"
    if model_path.exists() and not args.rerun:
        return model_path, "复用已有产物（--rerun 强制重跑）"
    if args.workspace:
        workspace = Path(args.workspace).resolve()
        entry = _run_pipeline(workspace, out_dir)
        if not model_path.exists():
            raise RuntimeError(
                f"{entry} 运行完成但 {model_path} 未产出（求解失败？"
                f"查看上方日志）")
        return model_path, f"已运行 {entry}"
    raise RuntimeError(
        f"{model_path} 不存在。提供 --workspace <dir> 运行管线，"
        "或 --out-dir 指向已有交付目录。")


# ---------------------------------------------------------------- L1 drawing

def audit_l1(model: dict, ctx: dict):
    """L1 契约：区域化 2D 构件——每根杆有 SourceRef + geometry_class，
    证据观测（label/dim）已登记。"""
    problems, report = [], {}
    bars = _model_bars(model)
    no_src = [c.get("id") for c in bars if not c.get("source")]
    no_class = [c.get("id") for c in bars
                if not (c.get("properties") or {}).get("geometry_class")]
    if no_src:
        problems.append(f"{len(no_src)}/{len(bars)} 根杆无 SourceRef（{no_src[:5]}…）")
    if no_class:
        problems.append(f"{len(no_class)}/{len(bars)} 根杆无 geometry_class（{no_class[:5]}…）")
    from collections import Counter
    gc = Counter((c.get("properties") or {}).get("geometry_class") for c in bars)
    census = _evidence_census(model)
    if bars and not census:
        problems.append("证据观测普查为空（label 观测未登记——evidence layer 断链）")
    elif bars and not (census.get("bar_label") or 0):
        problems.append("bar_label 观测为 0（件号证据层断链）")
    report["bars"] = len(bars)
    report["geometry_class"] = dict(gc)
    report["observations"] = census
    # 分册明细（deliver_project 路径有 sheets/）
    sheets_dir = ctx.get("sheets_dir")
    if sheets_dir and sheets_dir.is_dir():
        per_sheet = {}
        for p in sorted(sheets_dir.glob("*.json")):
            try:
                sm = _load_json(p)
                per_sheet[p.stem] = sum(
                    1 for c in (sm.get("components") or {}).values()
                    if c.get("kind") == "tower_bar")
            except ValueError:
                per_sheet[p.stem] = "unparsable"
        report["sheets"] = per_sheet
    return report, problems


# ---------------------------------------------------------------- L2 hypothesis

def audit_l2(model: dict, ctx: dict):
    """L2 契约：结构解释候选带四态状态机；观测普查完整。"""
    problems, report = [], {}
    hyps = [c for c in (model.get("components") or {}).values()
            if c.get("kind") == "hypothesis"]
    bad = [(c.get("id"), (c.get("properties") or {}).get("status"))
           for c in hyps
           if (c.get("properties") or {}).get("status") not in HYPOTHESIS_STATUSES]
    if bad:
        problems.append(f"{len(bad)} 个假设 status 非四态（{bad[:5]}…）")
    from collections import Counter
    st = Counter((c.get("properties") or {}).get("status") for c in hyps)
    census = _evidence_census(model)
    n_label = census.get("bar_label") or 0
    if n_label == 0 and _model_bars(model):
        problems.append("bar_label 观测为 0——L2 证据层断链")
    # 多册塔（overlay 声明 cross_file 合并）必须有假设产物
    overlay = ctx.get("overlay") or {}
    merge_stems = ((overlay.get("cross_file_views") or {}).get("stems")
                   or overlay.get("cross_file_stems"))
    if merge_stems and len(merge_stems) > 1 and not hyps:
        problems.append(
            f"overlay 声明 {len(merge_stems)} 册 cross-file 合并但假设数为 0"
            "（L2 结构解释层未产出）")
    report["hypotheses"] = len(hyps)
    report["status_histogram"] = dict(st)
    report["observations"] = census
    return report, problems


# ---------------------------------------------------------------- L3 rebuild

def audit_l3(model: dict, ctx: dict):
    """L3 契约：参数化补全可归因（geometry_origin），节点三轴求解。"""
    problems, report = [], {}
    bars = _model_bars(model)
    missing = [c.get("id") for c in bars
               if (c.get("properties") or {}).get("geometry_class")
               in CALIBER_ORIGINS_REQUIRED
               and not (c.get("properties") or {}).get("geometry_origin")]
    if missing:
        problems.append(
            f"{len(missing)}/{len(bars)} 根杆 geometry_class∈"
            f"{CALIBER_ORIGINS_REQUIRED} 但无 geometry_origin（口径纪律违例）")
    from collections import Counter
    origins = Counter((c.get("properties") or {}).get("geometry_origin")
                      for c in bars)
    nodes = _model_nodes(model)
    solved = sum(
        1 for n in nodes
        if (n.get("properties") or {}).get("solve_status") == "solved"
        or all((n.get("properties") or {}).get(k) is not None
               for k in ("x", "y", "z")))
    if nodes and solved == 0:
        problems.append(f"{len(nodes)} 个节点全部未求解（L3 重建失败）")
    side = sum(1 for c in bars if (c.get("properties") or {}).get("side_promoted"))
    report["bars"] = len(bars)
    report["geometry_origin"] = dict(origins)
    report["nodes"] = len(nodes)
    report["nodes_solved"] = solved
    report["side_promoted_bars"] = side
    return report, problems


# ---------------------------------------------------------------- L4 semantic-ir

def audit_l4(model: dict, ctx: dict):
    """L4 契约：公共 IR schema + BOM 交叉核验产物齐备。"""
    problems, report = [], {}
    for key in ("components", "dimensions", "connections", "rules"):
        if key not in model:
            problems.append(f"公共 IR 缺键 {key}")
    bom_rows = [c for c in (model.get("components") or {}).values()
                if c.get("kind") == "bom_row"]
    out_dir = ctx.get("out_dir")
    bom_tree = (_load_json(out_dir / "bom_tree.json")
                if (out_dir / "bom_tree.json").exists() else None)
    bar_inv = (_load_json(out_dir / "bar_inventory.json")
               if (out_dir / "bar_inventory.json").exists() else None)
    if ctx.get("bom_input") and not bom_rows and not bom_tree:
        problems.append(
            "提供了 BOM 输入但模型无 bom_row 组件、out 目录无 bom_tree.json"
            "（BOM 交叉核验层缺失——检查 classify_bom_row 行分类）")
    from collections import Counter
    rc = Counter((c.get("properties") or {}).get("row_class")
                 for c in bom_rows)
    report["bom_rows"] = len(bom_rows)
    report["row_class"] = dict(rc)
    if bom_tree:
        report["bom_tree_unique_ids"] = bom_tree.get("total_unique_bar_ids")
        report["bom_tree_conflicts"] = len(bom_tree.get("conflicts") or [])
        report["bom_tree_only_in_master"] = len(
            bom_tree.get("only_in_master") or [])
        report["bom_tree_only_in_model"] = len(
            bom_tree.get("only_in_model") or [])
    if bar_inv:
        report["bar_inventory_unique_ids"] = bar_inv.get("total_unique_bar_ids")
        report["bar_inventory_cross_sheet"] = bar_inv.get("cross_sheet_count")
    pd_path = out_dir / "project_delivery.json"
    if pd_path.exists():
        pd = _load_json(pd_path)
        report["physical_bar_counts"] = pd.get("physical_bar_counts")
    rules = model.get("rules") or {}
    for rid in ("r_bom_length_match", "r_bom_section_match"):
        if rid in rules:
            report[f"rule:{rid}"] = (rules[rid].get("status")
                                     if isinstance(rules[rid], dict) else rules[rid])
    return report, problems


# ---------------------------------------------------------------- L5 validation-gate

def audit_l5(model: dict, ctx: dict):
    """L5 契约：规则状态合法；failed/pending 逐条披露（gate 语义——
    全部 passed/review_exempted 才算过，pending 是诚实复核态不是失败）。"""
    problems, report = [], {}
    rules = model.get("rules") or {}
    if not rules:
        problems.append("模型无 rules（L5 规则裁决未运行）")
    pending, failed, passed, exempted, other = [], [], [], [], []
    for rid, r in rules.items():
        st = r.get("status") if isinstance(r, dict) else r
        if st == "passed":
            passed.append(rid)
        elif st == "pending":
            pending.append(rid)
        elif st == "failed":
            failed.append(rid)
        elif st == "review_exempted":
            exempted.append(rid)
        elif st == "not_applicable":
            other.append(rid)
        else:
            problems.append(f"规则 {rid} status={st!r} 非法")
            other.append(rid)
    if failed:
        problems.append(f"FAILED 规则 {len(failed)} 条：{failed}")
    if pending:
        problems.append(f"PENDING 规则 {len(pending)} 条（待人工复核）：{pending}")
    report["passed"] = passed
    report["failed"] = failed
    report["pending"] = pending
    report["review_exempted"] = exempted
    report["not_applicable"] = other
    out_dir = ctx.get("out_dir")
    for name in ("review_queue.json", "harness_summary.json"):
        if (out_dir / name).exists():
            report[name] = "present"
    return report, problems


# ---------------------------------------------------------------- L6 complete-tower

def audit_l6(model: dict, ctx: dict):
    """L6 契约：GLB 存在 + version.json 指纹链闭合
    （model_sha ↔ 磁盘、overlay_path ↔ overlay_sha）。"""
    problems, report = [], {}
    out_dir = ctx.get("out_dir")
    v_path = out_dir / "version.json"
    if not v_path.exists():
        problems.append(f"version.json 不存在（{v_path}）——L6 指纹链缺失")
        v = None
    else:
        v = _load_json(v_path)
        model_path = out_dir / "model.json"
        if v.get("model_sha") and model_path.exists():
            actual = _sha256(model_path)
            report["model_sha_match"] = (v["model_sha"] == actual)
            if v["model_sha"] != actual:
                problems.append(
                    "version.json model_sha 与磁盘 model.json 不一致"
                    "（产物改过后未重写 version.json）")
        ov_rel = v.get("overlay_path")
        if ov_rel:
            ov_path = Path(ov_rel)
            if not ov_path.is_absolute():
                ov_path = REPO / ov_path
            if not ov_path.exists():
                problems.append(f"version.json overlay_path 指向不存在的文件：{ov_path}")
            elif v.get("overlay_sha") and _sha256(ov_path) != v["overlay_sha"]:
                problems.append("version.json overlay_sha 与 overlay 文件不一致")
            report["overlay_path"] = ov_rel
        gt = (v.get("gt_injected") or {}).get("surfaces") or {}
        report["gt_injected_surfaces"] = sorted(gt.keys()) or "无（纯直读）"
        report["a2_blocks"] = [k for k in v if k.startswith("a2")] or None
    # GLB 存在性
    glbs = [p.name for p in out_dir.glob("*.glb")]
    n_bars = len(_model_bars(model))
    if n_bars and not glbs:
        problems.append(f"模型有 {n_bars} 根杆但 out 目录无任何 .glb（L6 导出缺失）")
    report["glb_files"] = glbs
    pd_path = out_dir / "project_delivery.json"
    if pd_path.exists():
        pd = _load_json(pd_path)
        report["delivery_status"] = pd.get("status")
    return report, problems


AUDITS = {1: audit_l1, 2: audit_l2, 3: audit_l3,
          4: audit_l4, 5: audit_l5, 6: audit_l6}


# ---------------------------------------------------------------- 入口

def resolve_layer(token: str) -> int:
    if token.isdigit():
        n = int(token)
        if n in LAYER_NAMES:
            return n
    for n, name in LAYER_NAMES.items():
        if token == name:
            return n
    raise SystemExit(f"未知层 {token!r}；可选：1..6 或 {sorted(LAYER_NAMES.values())}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("layer", help="1..6 或层名（drawing/hypothesis/rebuild/"
                    "semantic-ir/validation-gate/complete-tower）")
    ap.add_argument("--workspace", type=Path, default=None,
                    help="工作区目录（overlay.json + dxf/ + bom/）——产物缺失时运行管线")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="交付产物目录（审计模式，或工作区模式的产物落点）")
    ap.add_argument("--rerun", action="store_true",
                    help="产物已存在也强制重跑管线")
    args = ap.parse_args()

    n = resolve_layer(args.layer)
    name = LAYER_NAMES[n]

    if args.out_dir is None and args.workspace is None:
        print("需要 --workspace 或 --out-dir 之一", file=sys.stderr)
        return 2
    if args.out_dir is None:
        args.out_dir = args.workspace / "out"

    out_dir = args.out_dir.resolve()
    try:
        model_path, source = _ensure_artifacts(args, out_dir)
    except RuntimeError as exc:
        print(f"L{n} 前置失败：{exc}", file=sys.stderr)
        return 2

    model = _load_json(model_path)
    overlay = {}
    if args.workspace and (args.workspace / "overlay.json").exists():
        overlay = _load_json(args.workspace / "overlay.json")
    ctx = {
        "out_dir": out_dir,
        "sheets_dir": out_dir / "sheets",
        "overlay": overlay,
        "bom_input": bool(args.workspace and (args.workspace / "bom" / "bom.csv").exists()),
    }

    report, problems = AUDITS[n](model, ctx)
    report["_layer"] = {"n": n, "name": name, "model_source": source}
    audit_path = out_dir / f"layer{n}_{name}_audit.json"
    audit_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    print(f"=== L{n} {name} ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if problems:
        print(f"\nL{n} {name}: {len(problems)} 项违例")
        for p in problems:
            print("  ✗", p)
        print(f"审计报告：{audit_path}")
        return 1
    print(f"\nL{n} {name}: PASS（审计报告 {audit_path.name}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
