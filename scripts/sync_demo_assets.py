#!/usr/bin/env python3
"""Phase 6.2/6.4：demo 资产同步器（TASK_VIEWER_3D 任务 B.1 + POLISH 任务 6.4a）。

把 out/35A1-JC1-full-deliver/ 下的 viewer 数据资产拷进
web/demo/35A1-JC1/latest_deliver/，供 compare.html fetch。
独立于主管线（主管线每次 run 覆盖 tower_from_dxf.glb，与本脚本无交集）。

清单（核心必拷）：
    skeleton.glb            当前骨架（L 截面实体，provenance 五色）
    skeleton.bar_map.json   → bar_map.json（component_id ↔ role/origin 权威映射；
                            Phase 6.4：同步时读 model.json 把 section 并进每条记录，
                            非法格式记 null，viewer 据此做截面信息卡与规格分布）
    model.json              杆件/节点/坐标（统计面板现算用）
    metrics_multi_caliber.json / metrics_by_role.json / metrics_by_origin.json
    evidence_report.json    匹配对追溯
    review_queue.json       悬空复核节点（红球数据源）

清单（可选，缺失只警告不失败）：
    diff.glb / diff_report.json   （scripts/generate_diff_glb.py 产物）

用法：
    python3 scripts/sync_demo_assets.py [--src DIR] [--dst DIR]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = REPO_ROOT / "out/35A1-JC1-full-deliver"
DEFAULT_DST = REPO_ROOT / "web/demo/35A1-JC1/latest_deliver"

# Phase 6.4：合法角钢截面格式（任务书钉死）——L40X3 / Q345L100X7；
# 其它一律记 null（如 '-6X146' 钢板、'5M16X40' 螺栓规格污染，不进截面统计）。
import re as _re

def _section_re():
    return _re.compile(r"^(?:Q\d+)?L\d+(?:\.\d+)?X\d+(?:\.\d+)?$")

# (源文件名, 目标文件名, 是否必需)
ASSET_MANIFEST: List[Tuple[str, str, bool]] = [
    ("skeleton.glb", "skeleton.glb", True),
    ("skeleton.bar_map.json", "bar_map.json", True),
    ("model.json", "model.json", True),
    ("metrics_multi_caliber.json", "metrics_multi_caliber.json", True),
    ("metrics_by_role.json", "metrics_by_role.json", True),
    ("metrics_by_origin.json", "metrics_by_origin.json", True),
    ("evidence_report.json", "evidence_report.json", True),
    ("review_queue.json", "review_queue.json", True),
    ("diff.glb", "diff.glb", False),
    ("diff_report.json", "diff_report.json", False),
    # Phase 6.5：详图页（节点板样例数据源），缺失不影响其余资产
    ("sheets/35A1-JC1-03.json", "sheets/35A1-JC1-03.json", False),
]


def merge_section_into_bar_map(bar_map: List[dict], model: dict) -> List[dict]:
    """Phase 6.4：给 bar_map 每条记录并入 section 字段（解析失败/无关联 → null）。

    section 权威来源是 model.json 的 tower_bar.properties.section；
    按 component_id 关联。非法格式（非 L\d+X\d+ / Q\d+L\d+X\d+）记 null，
    防止螺栓规格、钢板规格污染截面统计（viewer 侧按 null 归「未关联」）。
    """
    sections = {}
    for cid, c in (model.get("components") or {}).items():
        if c.get("kind") == "tower_bar":
            sections[str(cid)] = (c.get("properties") or {}).get("section")
    rx = _section_re()
    out = []
    for e in bar_map:
        e2 = dict(e)
        raw = sections.get(str(e2.get("component_id")))
        raw = str(raw).strip() if raw is not None else ""
        e2["section"] = raw if raw and rx.match(raw) else None
        out.append(e2)
    return out


def sync_assets(src_dir: Path, dst_dir: Path) -> Dict[str, List[str]]:
    """按清单同步。返回 {"copied": [...], "skipped_optional": [...]}。

    必需文件缺失 → FileNotFoundError（管线产物没生成齐就该显式失败）。
    bar_map.json 特殊：不原样拷贝，而是读 model.json 把 section 并进每条记录
    （Phase 6.4）；JSON 结构异常时回退原样拷贝并在 warnings 里说明。
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    if not src_dir.is_dir():
        raise FileNotFoundError(f"源目录不存在：{src_dir}")
    dst_dir.mkdir(parents=True, exist_ok=True)

    copied: List[str] = []
    skipped: List[str] = []
    warnings: List[str] = []
    missing_required: List[str] = []
    for src_name, dst_name, required in ASSET_MANIFEST:
        src = src_dir / src_name
        if not src.exists():
            if required:
                missing_required.append(src_name)
            else:
                skipped.append(src_name)
            continue
        if src_name == "skeleton.bar_map.json":
            merged = _merge_bar_map_or_fallback(src, src_dir / "model.json", warnings)
            if merged is None:
                shutil.copy2(src, dst_dir / dst_name)
            else:
                (dst_dir / dst_name).write_text(
                    json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            (dst_dir / dst_name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / dst_name)
        copied.append(dst_name)

    if missing_required:
        raise FileNotFoundError(
            "缺少必需资产（先跑主管线 / generate_diff_glb）：" + ", ".join(missing_required))
    return {"copied": copied, "skipped_optional": skipped, "warnings": warnings}


def _merge_bar_map_or_fallback(bar_map_path: Path, model_path: Path,
                               warnings: List[str]):
    """读 bar_map + model 做 section 合并；结构异常返回 None（调用方回退原样拷贝）。"""
    try:
        bar_map = json.loads(bar_map_path.read_text(encoding="utf-8"))
        model = json.loads(model_path.read_text(encoding="utf-8"))
        if not isinstance(bar_map, list) or not isinstance(model, dict):
            raise TypeError("bar_map 需为 list / model 需为 dict")
        return merge_section_into_bar_map(bar_map, model)
    except (ValueError, TypeError, OSError) as e:
        warnings.append(f"bar_map section 合并失败（原样拷贝）：{e}")
        return None


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步 viewer 数据资产 out/ → web/demo/latest_deliver/")
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--dst", default=str(DEFAULT_DST))
    args = ap.parse_args(argv)
    try:
        result = sync_assets(Path(args.src), Path(args.dst))
    except FileNotFoundError as e:
        print(f"[sync_demo_assets] {e}", file=sys.stderr)
        return 2
    print(f"已同步 {len(result['copied'])} 个资产 → {args.dst}")
    for name in result["copied"]:
        print(f"  ✓ {name}")
    for name in result["skipped_optional"]:
        print(f"  ⚠ 可选缺失：{name}（diff 模式暂不可用，跑 scripts/generate_diff_glb.py 生成）")
    for w in result.get("warnings", []):
        print(f"  ⚠ {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
