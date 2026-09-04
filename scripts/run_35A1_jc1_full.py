#!/usr/bin/env python3
"""35A1-JC1 全册跑批：JC1 文件夹内所有图纸参与解析与工程追溯。

空间 3D 合并仅正立面 02（+ 合成侧视）；详图/模块页只进 M1 index，不进 M3。

默认走 deliver_project(agent_mode="ezdxf")；传 --agent-mode hybrid 则接 Kimi
Agent 链（A0 版面 → A2 几何 MLLM → A1 件号 MLLM → A3 关联 → A4 Harness），
六段塔身（02/04/05/06/07/40）经 MLLM 几何 + view_x/view_y 进 M3 合并成 30m 全高。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OVERLAY_PATH = REPO / "examples/external/guowang_35A1/layer_overlay.json"
DXF_BATCH = REPO / "out/xianyu-acceptance/batch-jc1/dxf"
OUT = REPO / "out/35A1-JC1-full-deliver"
DEMO_DIR = REPO / "web/demo/35A1-JC1"
GT_PATH = REPO / "examples/gt/35A1-JC1_ground_truth.json"


def full_overlay() -> dict:
    ov = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    cf = dict(ov.get("cross_file_views") or {})
    cf["parse_all_project_sheets"] = True
    ov["cross_file_views"] = cf
    return ov


def bar_stats(model) -> dict:
    from scripts.diagnose_35A1_jc1 import bar_graph_stats

    return bar_graph_stats(model)


def sheet_bar_summary(cross_file_dir: Path) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    for p in sorted(cross_file_dir.glob("35A1-JC1-*.json")):
        from traceability.io import load_model

        m = load_model(str(p))
        bars = sum(1 for c in m.components.values() if c.kind == "tower_bar")
        if bars:
            rows.append((p.stem, bars))
    return sorted(rows, key=lambda x: -x[1])


# P3-7：崩溃落盘路径 holder（main 赋值后生效）
_CRASH_OUT = [None]


def run_postprocess(out_dir: Path, repo: Path, overlay_path: Path,
                    demo_dir: Path) -> dict:
    """P0 收口流水线：review queue → diff → version.json → sync demo 资产。

    每步落盘 + 打印；任何一步失败都显式记录（不允许无声跳过，调用方把
    退出码抬到 2）。网页资产目录从此只由这条链决定，刷新即见最新模型。
    """
    import subprocess

    from traceability.project.versioning import write_version_manifest
    from scripts.sync_demo_assets import sync_assets

    postprocess: dict = {"steps": {}, "ok": True}

    def pp_step(name: str, fn) -> None:
        import traceback as _tb

        print(f"\n--- postprocess: {name} ---")
        try:
            detail = fn()
            postprocess["steps"][name] = {"ok": True, "detail": detail}
        except Exception as e:  # noqa: BLE001 — 收口步骤失败必须显式落盘
            postprocess["steps"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            postprocess["ok"] = False
            print(_tb.format_exc())

    def run_review_queue() -> str:
        r = subprocess.run(
            [sys.executable, str(repo / "scripts/generate_review_queue.py"),
             "--model", str(out_dir / "model.json"),
             "--out", str(out_dir / "review_queue.json")],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"generate_review_queue 退出码 {r.returncode}: {r.stderr[:400]}")
        return f"review_queue.json ({(out_dir / 'review_queue.json').stat().st_size} B)"

    def run_diff() -> str:
        old = repo / "out/35A1-JC1-baseline/model.json"
        if not old.exists():
            return "跳过：冻结基线缺失（diff 模式不可用）"
        if not (out_dir / "model.json").exists():
            return "跳过：主模型缺失（前序 deliver 失败，无 diff 可生成）"
        # P5 修复（2026-08-31）：diff 脚本的 DEFAULT_NEW 硬编码指向
        # full-deliver 目录——production/canonical 独立 out_dir 时它
        # 仍读旧 full-deliver 模型、把 diff.glb 写回 full-deliver，而
        # 本函数却在 out_dir 下 stat() → FileNotFoundError（postprocess
        # 未闭环的直接原因）。改为显式传 --new/--out-dir，diff 永远
        # 基于本次 out_dir 的模型生成、落在本次 out_dir 里。
        r = subprocess.run(
            [sys.executable, str(repo / "scripts/generate_diff_glb.py"),
             "--old", str(old),
             "--new", str(out_dir / "model.json"),
             "--out-dir", str(out_dir)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"generate_diff_glb 退出码 {r.returncode}: {r.stderr[:400]}")
        glb = out_dir / "diff.glb"
        if not glb.exists():
            raise RuntimeError(
                f"diff 生成器退出码 0 但未产出 {glb}（生成器内部路径异常）")
        return f"diff.glb ({glb.stat().st_size // 1024} KB)"

    def run_version() -> str:
        info = write_version_manifest(out_dir, repo, overlay_path)
        short = lambda s: (s or "")[:12]  # noqa: E731
        return (f"version.json run_id={short(info['run_id'])} "
                f"git={short(info.get('git_sha'))}{'*' if info.get('git_dirty') else ''} "
                f"model_sha={short(info.get('model_sha'))}")

    def run_sync() -> str:
        result = sync_assets(out_dir, demo_dir / "latest_deliver")
        if result.get("sha_mismatch"):
            raise RuntimeError(f"同步 SHA 不一致: {result['sha_mismatch']}")
        parts = [f"{len(result['copied'])} 个资产",
                 f"清理旧文件 {len(result.get('pruned', []))} 个"]
        return "，".join(parts)

    def run_dual_eval() -> str:
        # Bug E（2026-09-03，P2）：双视口径入交付产物。对外主口径
        # A2-dual-view-pure 与辅助 A2-dual-view-reconstructed 落盘
        # a2_dual_view.json（version.json 的 a2_dual_view 块从这里并入），
        # 75.8% 这类数字从此可从交付目录复核，不再只在 stdout。
        if not GT_PATH.exists():
            return "无 GT，跳过"
        r = subprocess.run(
            [sys.executable, str(REPO / "scripts/eval_a2_profiles.py"),
             str(GT_PATH), str(out_dir / "model.json")],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"eval_a2_profiles 退出码 {r.returncode}: {r.stderr[-300:]}")
        artifact = out_dir / "a2_dual_view.json"
        if not artifact.exists():
            raise RuntimeError("eval_a2_profiles 退出码 0 但未产出 a2_dual_view.json")
        import json as _json
        prof = _json.loads(artifact.read_text(encoding="utf-8"))["profiles"]
        p = prof["A2-dual-view-pure"]
        return (f"a2_dual_view.json 落盘（pure TP={p['TP']} P={p['P_pct']}% "
                f"R={p['R_pct']}%）")

    pp_step("review_queue", run_review_queue)
    pp_step("diff", run_diff)
    pp_step("dual_eval", run_dual_eval)
    pp_step("version", run_version)
    pp_step("sync", run_sync)
    return postprocess


def main() -> int:
    import argparse
    import subprocess
    import traceback

    from traceability.intake.tower_spec import cross_file_merge_stems
    from traceability.io import load_model
    from traceability.project.delivery import deliver_project
    from traceability.solve.tower_solver import tower_geometry_gate

    parser = argparse.ArgumentParser(description="35A1-JC1 全册交付")
    parser.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf",
                        help="几何提取后端：ezdxf（默认）/ hybrid（Kimi MLLM Agent 链）")
    parser.add_argument("--gt-align", action="store_true",
                        help="GT 权威拓扑对齐：用 .mod/.NODE 的 358 节点 + 1071 杆拓扑"
                             "重建 M3 骨架，使召回对齐 GT（100%）。默认关闭（纯 DXF 语义）。")
    parser.add_argument("--profile", choices=["canonical_assisted", "production_dxf"],
                        default="canonical_assisted",
                        help="P0.4 口径 profile：canonical_assisted（默认，研究对照，"
                             "use_gt_platform_levels=true——level-assisted TP 主要来源）；"
                             "production_dxf（生产真实能力，纯 DXF 平台层证据推导，"
                             "写独立目录 out/35A1-JC1-production/）")
    parser.add_argument("--dxf-dir", type=Path, default=None,
                        help="DXF 批次目录（默认 out/xianyu-acceptance/batch-jc1/dxf）")
    parser.add_argument("--dxf-stem-prefix", default="35A1-JC1",
                        help="DXF 文件名 stem 前缀白名单（D4 范围裁定：JC1 交付只吃 "
                             "35A1-JC1* 图册，批次目录里的 35C2-SJG1-ML 等异塔图纸"
                             "排除并记入 manifest 排除清单；空串 = 不过滤")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="交付输出目录（默认随 profile 变化）")
    parser.add_argument("--selection-mode", choices=["none", "p11", "relaxed"],
                        default=None,
                        help="06 斜材解释择优模式（覆盖 overlay diagonal_topology_selection_mode）")
    parser.add_argument("--skip-sync", action="store_true",
                        help="跳过 demo 资产同步（A/B 跑批时使用）")
    parser.add_argument("--break-source", choices=["gt", "level_grid"], default=None,
                        help="P2 D2a：腿链断链层来源。level_grid=LevelGridSolver 投票网格"
                             "（DXF 证据自推，无 GT 表）；默认 gt=原 GT terminal 表"
                             "（研究对照口径不变）。A/B 注入：脚本层覆盖 + 独立输出目录")
    parser.add_argument("--panel-source", choices=["gt", "level_grid"], default=None,
                        help="P2 D3：平台层来源。level_grid=marker 锚层（图纸梁标注，"
                             "非 GT 表）+纯 DXF 下游（横隔全量层/terminal_pair 关闭）；"
                             "默认 gt=GT 平台表（canonical 口径）")
    parser.add_argument("--side-prune-z", type=float, default=None,
                        help="02 专项：塔尖区 side 杆剪除边界覆盖（overlay "
                             "side_lift_prune_above_z_mm）。36601=保留全部塔头侧读杆；"
                             "默认 None=沿用 overlay 现值（34200，历史决策）")
    parser.add_argument("--side-keep-x-source", action="store_true",
                        help="02 专项：保留 x_source=z_pair 的 side 杆（overlay "
                             "side_lift_drop_x_source 置空）。默认关=沿用 overlay 剪除")
    parser.add_argument("--extra-bar-layer", default=None,
                        help="02 专项：向 bar_layers_by_stem['35A1-JC1-02'] 追加图层"
                             "（如 '0'——塔底/横担结构线实测在 layer 0）。"
                             "A/B 注入，不改共享 overlay")
    parser.add_argument("--side-extra-layer", action="store_true",
                        help="02 专项：side region 内补充图层收集（overlay "
                             "side_extra_bar_layers={layers:['0'], min_len_mm:100}）。"
                             "空间+长度双白名单——只收 side region 内 ≥100mm 物理"
                             "长的 layer 0 结构线，front 视图不受影响")
    args = parser.parse_args()

    overlay = full_overlay()
    overlay_path = OVERLAY_PATH
    out_dir = OUT
    # P3-7（2026-09-04）：崩溃落盘路径提前登记到模块级 holder——
    # main() 早段（参数解析/前置检查）崩溃时 out_dir 局部名未定义，
    # 旧崩溃兜底自身 NameError 被内层 except 吞掉，traceback 永不落盘。
    global _CRASH_OUT
    _CRASH_OUT[0] = out_dir
    dxf_batch = args.dxf_dir or DXF_BATCH
    if args.gt_align:
        # gt_align 只在脚本层开启，不改共享 overlay 文件，避免污染测试/其它调用方。
        overlay["gt_align"] = True
        tmp_overlay = OUT / "_overlay_gt_align.json"
        tmp_overlay.parent.mkdir(parents=True, exist_ok=True)
        tmp_overlay.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
        overlay_path = tmp_overlay
        print("GT 权威拓扑对齐: 开启（gt_align=True）")
    else:
        print("GT 权威拓扑对齐: 关闭（纯 DXF 提取语义）")

    if args.profile == "production_dxf":
        # P0.4：生产 profile 不改共享 overlay 文件（脚本层覆盖 + 独立输出目录）。
        # 纯 DXF 平台层（derive_panel_levels 证据推导），关闭 GT canonical 注入。
        # 消融实验入口（2026-08-31）：panel_level_source 可被 overlay 显式
        # 覆盖（例如隔离「GT 层位 vs DXF 层位」对 horiz_x 回归的归因）；
        # 其余生产语义（GT hw 关闭）不可覆盖。
        if "panel_level_source" not in overlay:
            overlay["panel_level_source"] = "dxf"
        overlay["use_gt_platform_levels"] = False
        overlay["use_gt_half_width"] = False
        out_dir = REPO / "out/35A1-JC1-production"
        tmp_overlay = out_dir / "_overlay_production.json"
        tmp_overlay.parent.mkdir(parents=True, exist_ok=True)
        tmp_overlay.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
        overlay_path = tmp_overlay
        print(f"Profile: production_dxf（panel_level_source={overlay['panel_level_source']}，GT 平台层注入关闭）")
    else:
        print("Profile: canonical_assisted（use_gt_platform_levels=true，研究对照口径）")

    if args.out_dir is not None:
        out_dir = args.out_dir

    if args.selection_mode:
        overlay["diagonal_topology_selection_mode"] = args.selection_mode

    _ab_dir_taken = False
    if args.break_source == "level_grid":
        # P2 D2a：断链层换投票网格。脚本层覆盖（共享 overlay 不动），
        # 独立输出目录，不进 demo 同步（A/B 对照跑批）。
        overlay["leg_chain_stitch_break_source"] = "level_grid"
        if not args.out_dir:
            out_dir = REPO / "out/35A1-JC1-lgrid"
        _ab_dir_taken = True
        args.skip_sync = True
        print(f"断链层来源: level_grid（投票网格，无 GT 表）→ {out_dir}")
    elif args.break_source == "gt":
        overlay["leg_chain_stitch_break_source"] = "gt"
        print("断链层来源: gt（GT terminal 表，原口径）")

    if args.panel_source == "level_grid":
        # P2 D3：平台层换 marker 锚层（图纸证据）+ 纯 DXF 下游语义。
        overlay["panel_level_source"] = "level_grid"
        if not args.out_dir and not _ab_dir_taken:
            out_dir = REPO / "out/35A1-JC1-pgrid"
        args.skip_sync = True
        print(f"平台层来源: level_grid（marker 锚层，纯 DXF 下游）→ {out_dir}")
    elif args.panel_source == "gt":
        overlay["panel_level_source"] = "gt"
        overlay["use_gt_platform_levels"] = True
        print("平台层来源: gt（GT 平台表，canonical 口径）")

    # 02 侧视专项：塔尖剪除边界 / z_pair 剪除的 A/B 覆盖（脚本层，
    # 不改共享 overlay）。历史决策（z≥34200 全 FP）在 sidegen x 合成
    # 质量差的前提下做出；重评实验用参数覆盖而非删规则。
    if args.side_prune_z is not None:
        overlay["side_lift_prune_above_z_mm"] = float(args.side_prune_z)
        if not args.out_dir and not _ab_dir_taken:
            out_dir = REPO / "out/35A1-JC1-sidep"
        _ab_dir_taken = True
        args.skip_sync = True
        print(f"side 塔尖剪除边界: {args.side_prune_z}（覆盖 overlay）→ {out_dir}")
    if args.side_keep_x_source:
        overlay["side_lift_drop_x_source"] = []
        if not args.out_dir and not _ab_dir_taken:
            out_dir = REPO / "out/35A1-JC1-sidek"
        _ab_dir_taken = True
        args.skip_sync = True
        print("side x_source=z_pair 剪除: 关闭（保留配对解算杆）")
    if args.extra_bar_layer:
        _bl = overlay.setdefault("bar_layers_by_stem", {})
        _cur = list(_bl.get("35A1-JC1-02") or [])
        for _lyr in str(args.extra_bar_layer).split(","):
            _lyr = _lyr.strip()
            if _lyr and _lyr not in _cur:
                _cur.append(_lyr)
        _bl["35A1-JC1-02"] = _cur
        if not args.out_dir and not _ab_dir_taken:
            out_dir = REPO / "out/35A1-JC1-sidel"
        _ab_dir_taken = True
        args.skip_sync = True
        print(f"02 册杆件图层追加: {args.extra_bar_layer} → {_cur}（A/B 注入）")
    if args.side_extra_layer:
        overlay["side_extra_bar_layers"] = {
            "layers": ["0"],
            "min_len_mm": 100.0,
            "stems": ["35A1-JC1-02"],
        }
        if not args.out_dir and not _ab_dir_taken:
            out_dir = REPO / "out/35A1-JC1-sidex"
        _ab_dir_taken = True
        args.skip_sync = True
        print("side region 图层补充收集: layer 0, ≥100mm, 仅 02 册（双白名单）")

    if (args.selection_mode or args.break_source or args.panel_source
            or args.side_prune_z is not None or args.side_keep_x_source
            or args.extra_bar_layer or args.side_extra_layer):
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_sel = out_dir / "_overlay_ab.json"
        tmp_sel.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
        overlay_path = tmp_sel
        if args.selection_mode:
            print(f"selection_mode: {args.selection_mode}")

    spatial_stems = sorted(cross_file_merge_stems(overlay))
    print("JC1 全册 DXF 目录:", dxf_batch)
    print("全册解析: parse_all_project_sheets=True")
    print("空间 3D 合并 stems:", ", ".join(spatial_stems))
    print("agent_mode:", args.agent_mode)

    if not dxf_batch.exists():
        print(f"DXF 目录不存在: {dxf_batch}", file=sys.stderr)
        return 1

    # D4 范围裁定：JC1 交付只吃 35A1-JC1* 图册。批次目录若混入异塔图纸
    # （实测 35C2-SJG1-ML.dxf 曾被目录级 glob 吞入，污染 model/bom_tree/
    # bar_inventory），按 stem 前缀白名单过滤到干净临时目录（符号链接，
    # 不复制文件），排除清单打印留档（审计可追溯，不静默丢弃）。
    stem_prefix = args.dxf_stem_prefix.strip()
    if stem_prefix:
        all_dxf = sorted(dxf_batch.glob("*.dxf"))
        keep = [p for p in all_dxf if p.stem.startswith(stem_prefix)]
        drop = [p for p in all_dxf if not p.stem.startswith(stem_prefix)]
        if drop:
            print(f"D4 范围过滤：排除 {len(drop)} 个非 {stem_prefix}* 文件：")
            for p in drop:
                print(f"  - {p.name}")
        if not keep:
            print(f"过滤后无匹配 DXF（前缀 {stem_prefix}）", file=sys.stderr)
            return 1
        scope_dir = out_dir / "_dxf_scope"
        scope_dir.mkdir(parents=True, exist_ok=True)
        for old in scope_dir.glob("*.dxf"):
            old.unlink()
        for p in keep:
            link = scope_dir / p.name
            if not link.exists():
                link.symlink_to(p.resolve())
        dxf_batch = scope_dir
        print(f"D4 范围过滤：{len(keep)}/{len(all_dxf)} 个 DXF 进入交付")

    out_dir.mkdir(parents=True, exist_ok=True)
    pd = deliver_project(
        dxf_batch,
        layer_map_path=str(overlay_path),
        bom_path=str(REPO / "examples/external/guowang_35A1/guowang_merged_bom.csv"),
        project_id="35A1-JC1-full",
        out_dir=out_dir,
        agent_mode=args.agent_mode,
    )

    model = load_model(str(out_dir / "model.json"))
    stats = bar_stats(model)
    gate = tower_geometry_gate(model, str(OVERLAY_PATH))

    from traceability.eval.generation_status import collect_generation_status
    _model_dict = json.loads((out_dir / "model.json").read_text(encoding="utf-8"))
    gen_status = collect_generation_status(_model_dict)
    (out_dir / "generation_status.json").write_text(
        json.dumps(gen_status, ensure_ascii=False, indent=2), encoding="utf-8")

    sheet_rows = sheet_bar_summary(out_dir / "cross_file")
    total_sheet_bars = sum(b for _, b in sheet_rows)

    print()
    print("=== 35A1-JC1 全册交付 ===")
    print(f"deliver ok: {pd.get('ok')}")
    print(f"deliver status: {pd.get('status')}")
    print(f"空间 merge_stems: {pd.get('merge_report', {}).get('merge_stems')}")
    print(f"全册有杆件的分册: {len(sheet_rows)} 张，合计 {total_sheet_bars} 根（各分册独立解析）")
    print(f"3D 合并模型杆件: {stats['bars']}")
    print(f"3D 节点: {stats['nodes']}")
    print(f"塔高 bbox Z: {stats.get('bbox_mm', {}).get('z', '未解算')}")
    print(f"门禁: {'通过' if gate['ok'] else '失败'}")
    if gate.get("reasons"):
        for r in gate["reasons"]:
            print(f"  - {r}")
    # P0.1 结构化状态链：四个子阶段并列展示，消除「门禁通过但 failed」的
    # 表面矛盾——几何门禁与证据校验各自独立，failed 必有 failure_reasons。
    ss = pd.get("stage_status") or {}
    if ss:
        print("状态链: " + "  ".join(
            f"{name}={'ok' if (ss.get(name) or {}).get('ok') else 'NG'}"
            for name in ("gate", "validation", "export", "evidence")
        ))
    for fr in (pd.get("failure_reasons") or []):
        print(f"  ✗ [{fr.get('code')}] ({fr.get('stage')}) {fr.get('message')}")
    for rr in (pd.get("review_reasons") or [])[:5]:
        print(f"  ⚠ [{rr.get('code')}] ({rr.get('stage')}) {rr.get('message')}")

    if GT_PATH.exists():
        bom_file = REPO / "examples/external/guowang_35A1/guowang_merged_bom.csv"
        ev_cmd = [
            sys.executable, str(REPO / "scripts/evaluate_ground_truth.py"),
            str(GT_PATH), str(out_dir / "model.json"), "--view", "front",
        ]
        if bom_file.exists():
            ev_cmd.extend(["--bom", str(bom_file)])
        ev = subprocess.run(
            ev_cmd,
            capture_output=True, text=True,
        )
        print()
        print("=== GT 评测（3D 合并模型）===")
        for ln in ev.stdout.strip().splitlines():
            print(ln)

    # Phase A3：skeleton.glb 是 M3 骨架主产物（P4 已删除 tower.glb 兼容别名）。
    skeleton = out_dir / "skeleton.glb"
    glb = skeleton if skeleton.exists() else None
    if glb and glb.exists():
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(glb, DEMO_DIR / "tower_from_dxf.glb")
        print(f"\nGLB(M3 skeleton): {glb} ({glb.stat().st_size // 1024} KB)")
        print(f"预览: http://127.0.0.1:8000/demo/35A1-JC1/compare.html")

    report = {
        "spatial_merge_stems": spatial_stems,
        "sheet_bar_summary": sheet_rows,
        "total_sheet_bars": total_sheet_bars,
        "deliver": pd,
        "stats_3d": stats,
        "gate": gate,
    }
    (out_dir / "full_run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"报告: {out_dir / 'full_run_report.json'}")

    # ------------------------------------------------------------------
    # P0 收口流水线：run full → review queue → diff → version.json → sync。
    # 每步落盘 + 打印，任何一步失败都显式进入 postprocess 状态（不允许无声跳过，
    # 结束码抬到 2），网页资产目录从此只由这条链决定。
    # P0.4：production_dxf profile 不污染演示资产目录（demo 只跟
    # canonical_assisted 主线产物）。
    # ------------------------------------------------------------------
    postprocess = run_postprocess(
        out_dir, REPO, overlay_path,
        DEMO_DIR if args.profile == "canonical_assisted" and not args.skip_sync
        else (out_dir / "_no_demo_sync"))
    report["postprocess"] = postprocess
    report["profile"] = args.profile
    (out_dir / "full_run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if not postprocess["ok"]:
        print("\n✗ postprocess 未全部成功："
              + ", ".join(k for k, v in postprocess["steps"].items() if not v["ok"]))

    # P0-2 失败传播：verified → 0，review_required → 1，failed → 2。
    status = pd.get("status", "failed")
    exit_code = {"verified": 0, "review_required": 1, "failed": 2}.get(status, 2)
    if not postprocess["ok"]:
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    import traceback
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        # 任何未捕获异常都落盘 traceback，避免「无声退出、log 为空」。
        print(tb, flush=True)
        try:
            _co = _CRASH_OUT[0]
            if _co is not None:
                _co.mkdir(parents=True, exist_ok=True)
                (_co / "crash_traceback.log").write_text(tb, encoding="utf-8")
                print(f"崩溃 traceback 已写: {_co / 'crash_traceback.log'}", flush=True)
        except Exception:
            pass
        raise
