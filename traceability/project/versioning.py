"""P0 版本固定：run_id / git_sha / model_sha 运行信息清单（version.json）。

解决的问题：代码已升级、产物已生成，但网页无法判定「现在显示的是不是
最新模型」。每次跑批收口时把版本指纹写入 out/<deliver>/version.json，
由 sync_demo_assets 同步进 viewer 资产目录；compare.html 顶部显示，
并用 crypto.subtle 在浏览器端实测 model.json / skeleton.glb 的 SHA-256
与 version.json 中的指纹比对（P0.4 验收：网页 SHA == out SHA）。

字段命名遵循用户任务书：
    run_id / git_sha / overlay_sha / model_sha / generated_at /
    model_components / model_nodes
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

A2_CALIBERS = ("pure", "reconstructed", "level_assisted", "parametric", "full")
A2_TOL_MM = 500.0

BASE_NOTE = ("底段 z<6500 无完整 DXF 图源：基于 07 段收分规律的参数化外推，"
             "geometry_class=derived_parametric，不计入 A2-pure，非 GT 坐标注入")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_head_info(repo_root: Path) -> dict:
    """当前 HEAD 的 sha + 工作区是否 dirty。git 不可用时返回 error 说明。"""
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip() or None
        dirty_out = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return {"sha": sha, "dirty": bool(dirty_out.strip())}
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
        return {"sha": None, "dirty": None, "error": str(e)}


def _sweep_at(caliber: Optional[dict], tol: float) -> Optional[dict]:
    if not caliber:
        return None
    for s in caliber.get("sweep") or []:
        if abs(float(s.get("tol", -1)) - tol) < 1e-6:
            return s
    return None


def a2_summary(metrics_path: Path) -> dict:
    """从 metrics_multi_caliber.json 提取各口径 TP@500 / P / R 摘要。"""
    out: dict = {}
    if not metrics_path.exists():
        return out
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return out
    calibers = metrics.get("calibers") or {}
    for cal in A2_CALIBERS:
        s = _sweep_at(calibers.get(cal), A2_TOL_MM)
        if s:
            out[cal] = {
                "tp500": s.get("tp"),
                "n_model": calibers[cal].get("n_model"),
                "precision": s.get("precision"),
                "recall": s.get("recall"),
            }
    eff = metrics.get("effective") or {}
    s = _sweep_at(eff, A2_TOL_MM)
    if s:
        out["effective"] = {
            "tp500": s.get("tp"),
            "precision": s.get("precision"),
            "recall": s.get("recall"),
            "z_min_mm": eff.get("z_min_mm"),
        }
    return out


def model_counts(model: dict) -> dict:
    """杆件/节点计数 + Z 范围 + 参数化底段统计。"""
    bars = 0
    nodes = 0
    zmin: Optional[float] = None
    zmax: Optional[float] = None
    param_bars = 0
    param_zmin: Optional[float] = None
    param_zmax: Optional[float] = None
    observations: Dict[str, int] = {}
    hypotheses: Dict[str, int] = {}
    for c in (model.get("components") or {}).values():
        props = c.get("properties") or {}
        if c.get("kind") == "tower_bar":
            bars += 1
            if props.get("geometry_class") == "derived_parametric" or \
                    str(props.get("geometry_origin", "")).startswith("derived_parametric"):
                param_bars += 1
                for node_id in (c.get("nodes") or []):
                    n = (model.get("components") or {}).get(str(node_id)) or {}
                    z = (n.get("properties") or {}).get("z")
                    if z is not None:
                        z = float(z)
                        param_zmin = z if param_zmin is None else min(param_zmin, z)
                        param_zmax = z if param_zmax is None else max(param_zmax, z)
        elif c.get("kind") == "tower_node":
            nodes += 1
            z = props.get("z")
            if z is not None:
                z = float(z)
                zmin = z if zmin is None else min(zmin, z)
                zmax = z if zmax is None else max(zmax, z)
        elif c.get("kind") == "observation":
            ok = str(props.get("observation_kind") or "unknown")
            observations[ok] = observations.get(ok, 0) + 1
        elif c.get("kind") == "hypothesis":
            hs = str(props.get("status") or "proposed")
            hypotheses[hs] = hypotheses.get(hs, 0) + 1
    base: dict = {"bars": param_bars, "note": BASE_NOTE}
    if param_zmin is not None:
        base["z_range_mm"] = [param_zmin, param_zmax]
    result = {
        "model_components": bars,
        "model_nodes": nodes,
        "z_range_mm": [zmin, zmax] if zmin is not None else None,
        "base_segment": base,
    }
    # P0 架构对齐（2026-09-05 审计）：证据层计数（观测按子类、假设按四态）
    if observations:
        result["observations"] = observations
    if hypotheses:
        result["hypotheses"] = hypotheses
    return result


def collect_version_info(out_dir: Path, repo_root: Path,
                         overlay_path: Optional[Path] = None) -> dict:
    """汇总一次跑批的版本指纹。out_dir 下应有 model.json / skeleton.glb /
    run_manifest.json / metrics_multi_caliber.json。"""
    out_dir = Path(out_dir)
    repo_root = Path(repo_root)
    overlay_path = Path(overlay_path) if overlay_path else \
        repo_root / "examples/external/guowang_35A1/layer_overlay.json"

    run_id: Optional[str] = None
    manifest_path = out_dir / "run_manifest.json"
    if manifest_path.exists():
        try:
            run_id = (json.loads(manifest_path.read_text(encoding="utf-8"))
                      .get("run_id"))
        except (ValueError, OSError):
            run_id = None
    if not run_id:
        run_id = uuid.uuid4().hex

    info: dict = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    git = git_head_info(repo_root)
    info["git_sha"] = git.get("sha")
    info["git_dirty"] = git.get("dirty")
    if git.get("error"):
        info["git_error"] = git["error"]
    if overlay_path.exists():
        info["overlay_sha"] = sha256_file(overlay_path)
        # P0 审计（2026-09-05）：GT z-only 注入面在 version.json 里显式标注，
        # 「默认开着且不标注」的状态结束。列出的键 = 该跑批实际启用的
        # GT 注入面（overlay 声明为准），对应评测口径需带 level-assisted
        # 说明（A2-dual-view-pure 等纯直读口径不包含这些注入）。
        try:
            _ov = json.loads(overlay_path.read_text(encoding="utf-8"))
            _gt_keys = [
                "use_gt_platform_levels",
                "use_gt_half_width",
                "use_gt_diaphragm_levels",
            ]
            _active: dict = {k: _ov.get(k) for k in _gt_keys
                             if _ov.get(k) not in (None, False)}
            # 跨度表只记条数不记全表（version.json 保持精简，
            # 全表在 overlay 里，overlay_sha 已固定指纹）
            _wl = _ov.get("terminal_pair_span_whitelist")
            if isinstance(_wl, list) and _wl:
                _active["terminal_pair_span_whitelist"] = f"{len(_wl)} pairs"
            if _ov.get("panel_level_source") == "gt":
                _active["panel_level_source"] = "gt"
            if _active:
                info["gt_injected"] = {
                    "surfaces": _active,
                    "note": ("z-only 设计常数注入（层表/跨度表）；x/y 严禁注入。"
                             "含 level_source=gt 的跑批为 level-assisted 口径，"
                             "与纯直读口径（A2-dual-view-pure）区分呈报。"),
                }
        except (ValueError, OSError):
            pass

    model_path = out_dir / "model.json"
    if model_path.exists():
        info["model_sha"] = sha256_file(model_path)
        try:
            info.update(model_counts(
                json.loads(model_path.read_text(encoding="utf-8"))))
        except (ValueError, OSError) as e:
            info["model_parse_error"] = str(e)
    skeleton = out_dir / "skeleton.glb"
    if skeleton.exists():
        info["skeleton_sha"] = sha256_file(skeleton)

    info["a2"] = a2_summary(out_dir / "metrics_multi_caliber.json")
    return info


def write_version_manifest(out_dir: Path, repo_root: Path,
                           overlay_path: Optional[Path] = None) -> dict:
    """收集版本指纹并写 out_dir/version.json。返回写入的 info。"""
    info = collect_version_info(out_dir, repo_root, overlay_path)
    path = Path(out_dir) / "version.json"
    path.write_text(json.dumps(info, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return info
