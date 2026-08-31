#!/usr/bin/env python3
"""Phase 6.2：demo 资产同步器（TASK_VIEWER_3D 任务 B.1）。

把 out/35A1-JC1-full-deliver/ 下的 viewer 数据资产拷进
web/demo/35A1-JC1/latest_deliver/，供 compare.html fetch。
独立于主管线（主管线每次 run 覆盖 tower_from_dxf.glb，与本脚本无交集）。

清单（核心必拷）：
    skeleton.glb            当前骨架（L 截面实体，provenance 五色）
    skeleton.bar_map.json   → bar_map.json（component_id ↔ role/origin 权威映射）
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
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = REPO_ROOT / "out/35A1-JC1-full-deliver"
DEFAULT_DST = REPO_ROOT / "web/demo/35A1-JC1/latest_deliver"

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
]


def sync_assets(src_dir: Path, dst_dir: Path) -> Dict[str, List[str]]:
    """按清单同步。返回 {"copied": [...], "skipped_optional": [...]}。

    必需文件缺失 → FileNotFoundError（管线产物没生成齐就该显式失败）。
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    if not src_dir.is_dir():
        raise FileNotFoundError(f"源目录不存在：{src_dir}")
    dst_dir.mkdir(parents=True, exist_ok=True)

    copied: List[str] = []
    skipped: List[str] = []
    missing_required: List[str] = []
    for src_name, dst_name, required in ASSET_MANIFEST:
        src = src_dir / src_name
        if not src.exists():
            if required:
                missing_required.append(src_name)
            else:
                skipped.append(src_name)
            continue
        shutil.copy2(src, dst_dir / dst_name)
        copied.append(dst_name)

    if missing_required:
        raise FileNotFoundError(
            "缺少必需资产（先跑主管线 / generate_diff_glb）：" + ", ".join(missing_required))
    return {"copied": copied, "skipped_optional": skipped}


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
