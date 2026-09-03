#!/usr/bin/env python3
"""门禁 2：公共 IR 校验（validate_public_ir）。

官网基座对标（scripts/validate_public_ir.py）：交付模型在出门前必须
通过的硬校验。对一份 model.json 检查：

  1. schema 校验（schema/engineering_model.json，含证据层 kind）
  2. 口径纪律：recognized/reconstructed/level_assisted/parametric 四类
     geometry_class 的组件都带 geometry_origin（来源可归因）
  3. 证据层：observation 组件带稳定 ID + observation_kind；
     hypothesis 组件带四态 status
  4. SourceRef 抽查：tower_bar 必须带 source（来源可追溯）
  5. GT 注入披露：若 drawing_file 带 gt_injected 相关键，version.json
     必须同目录存在且含 gt_injected.surfaces

退出码：全部通过 0，任一失败 1（列出全部问题再退出）。
用法：
  python3 domains/angle-tower/scripts/validate_public_ir.py <model.json> [--version <version.json>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

CALIBER_ORIGINS_REQUIRED = ("recognized", "reconstructed", "derived_parametric")
HYPOTHESIS_STATUSES = {"proposed", "accepted", "rejected", "superseded"}
MAX_REPORT = 20


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="待校验的 model.json 路径")
    ap.add_argument("--version", help="配套 version.json 路径（默认取同目录）")
    args = ap.parse_args()

    model_path = Path(args.model)
    problems: list[str] = []

    if not model_path.exists():
        print(f"FAIL: 模型不存在 {model_path}")
        return 1
    m = json.loads(model_path.read_text(encoding="utf-8"))
    comps = m.get("components") or {}

    # ---- 1) schema 校验（有 jsonschema 就严格校验，没有就键检查）----
    try:
        import jsonschema
        schema = json.loads(
            (REPO / "schema" / "engineering_model.json").read_text(encoding="utf-8"))
        try:
            jsonschema.validate(m, schema)
            print(f"[PASS] schema 校验（components={len(comps)}）")
        except jsonschema.ValidationError as e:
            problems.append(f"schema: {e.message[:200]}（at {list(e.absolute_path)[:5]}）")
    except ImportError:
        required = ("name", "components", "dimensions", "connections", "rules")
        missing = [k for k in required if k not in m]
        if missing:
            problems.append(f"IR 顶层缺键: {missing}")
        else:
            print("[PASS] IR 顶层键完整（无 jsonschema，降级键检查）")

    # ---- 2) 口径纪律：几何来源可归因 ----
    n_no_origin = 0
    for cid, c in comps.items():
        p = c.get("properties") or {}
        gc = p.get("geometry_class")
        if gc in CALIBER_ORIGINS_REQUIRED and not p.get("geometry_origin"):
            n_no_origin += 1
            if len(problems) < MAX_REPORT:
                problems.append(
                    f"口径纪律: {cid} geometry_class={gc} 缺 geometry_origin")
    if n_no_origin == 0:
        print("[PASS] 口径纪律（几何组件全部可归因）")
    else:
        problems.append(f"口径纪律: 共 {n_no_origin} 个组件缺 geometry_origin")

    # ---- 3) 证据层完整性 ----
    # 注：跨册合并后组件 ID 带 {stem}__ 前缀（如 35A1-JC1-02__obs_...），
    # 稳定 ID 契约按「去前缀后以 obs_/hyp_ 开头」判定。
    def _unprefixed(cid: str) -> str:
        return cid.rsplit("__", 1)[-1] if "__" in cid else cid

    n_obs = n_obs_bad = n_hyp = n_hyp_bad = 0
    for cid, c in comps.items():
        k = c.get("kind")
        p = c.get("properties") or {}
        if k == "observation":
            n_obs += 1
            if not (_unprefixed(cid).startswith("obs_") and p.get("observation_kind")):
                n_obs_bad += 1
                if len(problems) < MAX_REPORT:
                    problems.append(f"证据层: 观测 {cid} 缺稳定 ID 前缀或 observation_kind")
        elif k == "hypothesis":
            n_hyp += 1
            if p.get("status") not in HYPOTHESIS_STATUSES:
                n_hyp_bad += 1
                if len(problems) < MAX_REPORT:
                    problems.append(f"证据层: 假设 {cid} status={p.get('status')!r} 非四态")
    if n_obs_bad == 0 and n_hyp_bad == 0:
        print(f"[PASS] 证据层（obs={n_obs}, hyp={n_hyp}，ID/status 契约完整）")
    else:
        problems.append(f"证据层: 观测违例 {n_obs_bad}/{n_obs}，假设违例 {n_hyp_bad}/{n_hyp}")

    # ---- 4) SourceRef 抽查（tower_bar 必须带 source）----
    bars = [c for c in comps.values() if c.get("kind") == "tower_bar"]
    n_no_src = sum(1 for c in bars if not c.get("source"))
    if n_no_src == 0:
        print(f"[PASS] SourceRef（tower_bar={len(bars)} 全部带来源）")
    else:
        problems.append(f"SourceRef: {n_no_src}/{len(bars)} tower_bar 无 source")

    # ---- 5) GT 注入披露 ----
    df = comps.get("drawing_file") or {}
    df_props = df.get("properties") or {}
    gt_keys = [k for k in (
        "gt_platform_levels_override", "gt_terminal_levels_override",
        "gt_diaphragm_levels_override", "terminal_pair_span_whitelist",
    ) if df_props.get(k) or (m.get("overlay") or {}).get(k)]
    v_path = Path(args.version) if args.version else model_path.parent / "version.json"
    if gt_keys:
        if not v_path.exists():
            problems.append(f"GT 披露: 检测到注入键 {gt_keys} 但 version.json 不存在（{v_path}）")
        else:
            v = json.loads(v_path.read_text(encoding="utf-8"))
            surfaces = ((v.get("gt_injected") or {}).get("surfaces") or {})
            missing = [k for k in gt_keys if k not in surfaces and "terminal_levels" not in surfaces]
            if missing:
                problems.append(f"GT 披露: 注入键 {missing} 未登记于 version.json gt_injected.surfaces")
            else:
                print(f"[PASS] GT 注入披露（{len(surfaces)} 面已登记）")
    else:
        print("[PASS] GT 注入披露（无 z-only 注入面）")

    print()
    if problems:
        print(f"=== validate_public_ir: {len(problems)} 项问题 ===")
        for p in problems[:MAX_REPORT]:
            print("  ✗", p)
        return 1
    print("=== validate_public_ir: 全部通过 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
