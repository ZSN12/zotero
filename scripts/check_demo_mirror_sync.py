#!/usr/bin/env python3
"""镜像一致性检查：web/demo/<塔>/latest_deliver == out/<canonical>（sha 级）。

背景（2026-09-05 审计缺口）：web/demo/ 是用户实际打开网页看到的交付，
out/ 是内部交付——两处由 sync_demo_assets.py 手工同步，out/ 重跑后镜像
不跟走（实测镜像停在 09-03 的 220/旧 overlay），且出现过跨塔指纹污染
（ZC1 镜像带 JC1 的 overlay_sha256=81df6343…）。out/ 与 web/demo/**
均为 gitignore 产物，CI 环境不存在——故本脚本双通道落地：

  1. 本地/交付机：直接对真实目录跑（本文件主体）；
  2. CI：tests/test_demo_mirror_sync.py 用合成夹具验证检查逻辑
     （同步在 pytest 快层，即 ci.yml "tests" job）。

检查项（每塔，src=out canonical，dst=web/demo 镜像）：
  A. 必需清单文件缺失 → FAIL；
  B. 镜像文件 sha256 != src 同名文件 sha256 → FAIL（过期/损坏）；
  C. 镜像 a2_dual_view.json 的 eval_binding.overlay_sha256 必须 ==
     本塔 version.json.overlay_path 所指 overlay 文件的真实 sha256
     （错配=用了别塔的 overlay 评测）；
  D. 跨塔污染：不同塔的 overlay 文件不同 → overlay_sha256 必须不同；
  E. 镜像 version.json 必须 == src version.json（run_id/git_sha 一致）。

用法：python3 scripts/check_demo_mirror_sync.py [--strict-skip]
  --strict-skip 把「src 不存在→跳过」也计为失败（交付机自检用）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# (塔名, out canonical 目录, web/demo 镜像目录)
TOWERS: List[Tuple[str, Path, Path]] = [
    ("35A1-JC1",
     REPO_ROOT / "out/35A1-JC1-full-deliver",
     REPO_ROOT / "web/demo/35A1-JC1/latest_deliver"),
    ("35A2-ZC1",
     REPO_ROOT / "out/35A2-ZC1-full-deliver",
     REPO_ROOT / "web/demo/35A2-ZC1/latest_deliver"),
]

# 与 sync_demo_assets.py 清单一致：(src 名, 镜像名, 必需)。
# 注意 skeleton.bar_map.json 同步时改名 bar_map.json。
REQUIRED_FILES = [
    ("version.json", "version.json"),
    ("model.json", "model.json"),
    ("skeleton.glb", "skeleton.glb"),
    ("skeleton.bar_map.json", "bar_map.json"),  # 变换写出（见下）
    ("metrics_multi_caliber.json", "metrics_multi_caliber.json"),
    ("metrics_by_role.json", "metrics_by_role.json"),
    ("metrics_by_origin.json", "metrics_by_origin.json"),
    ("evidence_report.json", "evidence_report.json"),
    ("review_queue.json", "review_queue.json"),
]
OPTIONAL_FILES = [
    ("a2_dual_view.json", "a2_dual_view.json"),
    ("diff.glb", "diff.glb"),
    ("diff_report.json", "diff_report.json"),
    ("canonical.glb", "canonical.glb"),
    ("canonical.bar_map.json", "canonical.bar_map.json"),
]


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_tower(name: str, src: Path, dst: Path) -> Tuple[List[str], List[str]]:
    """返回 (failures, notes)。failures 非空 = 该塔不通过。"""
    fails: List[str] = []
    notes: List[str] = []
    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)

    if not src.is_dir():
        return [], [f"{name}: src 不存在（{_rel(src)}）→ 跳过"]
    if not dst.is_dir():
        return [f"{name}: 镜像目录缺失 {_rel(dst)}"], notes

    # A/B：清单文件逐项 sha 对比（src 名 → 镜像名映射）。
    # bar_map.json 例外：sync 时会并入 section 字段后重写（确定性变换），
    # 检查器重放同款变换（merge_section_into_bar_map + json.dumps
    # ensure_ascii=False indent=1）后做字节级比对——sha 直比是误报。
    import json as _json
    for sfn, dfn in REQUIRED_FILES:
        s, d = src / sfn, dst / dfn
        if not s.exists():
            fails.append(f"{name}: src 缺必需文件 {sfn}")
            continue
        if not d.exists():
            fails.append(f"{name}: 镜像缺必需文件 {dfn}")
            continue
        if sfn == "skeleton.bar_map.json":
            try:
                bar_map = _json.loads(s.read_text(encoding="utf-8"))
                model = _json.loads((src / "model.json").read_text(encoding="utf-8"))
                from sync_demo_assets import merge_section_into_bar_map
                expected = merge_section_into_bar_map(bar_map, model)
                expected_bytes = _json.dumps(
                    expected, ensure_ascii=False, indent=1).encode("utf-8")
                actual_bytes = d.read_bytes()
                if expected_bytes != actual_bytes:
                    fails.append(
                        f"{name}: bar_map.json 与源+变换结果不一致（镜像过期）")
            except Exception as e:  # noqa: BLE001
                fails.append(f"{name}: bar_map.json 变换比对失败 {e}")
        elif _sha(s) != _sha(d):
            fails.append(f"{name}: {dfn} sha 不一致（镜像过期或损坏）")
    for sfn, dfn in OPTIONAL_FILES:
        s, d = src / sfn, dst / dfn
        if s.exists() and d.exists() and _sha(s) != _sha(d):
            fails.append(f"{name}: {dfn} sha 不一致（镜像过期或损坏）")
        elif s.exists() and not d.exists():
            notes.append(f"{name}: src 有 {sfn} 但镜像缺（可选，建议重同步）")

    # C：镜像 overlay 指纹 == 本塔 overlay 文件真实 sha
    vjson = src / "version.json"
    if vjson.exists() and (dst / "a2_dual_view.json").exists():
        try:
            v = json.loads(vjson.read_text())
            ov_path = REPO_ROOT / str(v.get("overlay_path") or "")
            m = json.loads((dst / "a2_dual_view.json").read_text())
            m_ov = (m.get("eval_binding") or {}).get("overlay_sha256")
            if ov_path.exists() and m_ov:
                real = _sha(ov_path)
                if m_ov != real:
                    fails.append(
                        f"{name}: 镜像 a2_dual_view.overlay_sha256={m_ov[:12]}… "
                        f"≠ 本塔 overlay 真实 sha={real[:12]}…（评测用错 overlay/跨塔污染）")
        except (json.JSONDecodeError, OSError) as e:
            fails.append(f"{name}: 指纹校验读取失败 {e}")

    # E：version.json 必须逐字节一致（run_id/git_sha 同源）
    if (src / "version.json").exists() and (dst / "version.json").exists():
        if _sha(src / "version.json") != _sha(dst / "version.json"):
            fails.append(f"{name}: version.json 不一致（run_id/git_sha 漂移）")

    return fails, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict-skip", action="store_true",
                    help="src 缺失的塔也计失败（交付机自检）")
    args = ap.parse_args()

    all_fails: List[str] = []
    all_notes: List[str] = []
    overlay_shas: Dict[str, str] = {}
    for name, src, dst in TOWERS:
        fails, notes = check_tower(name, src, dst)
        all_fails += fails
        all_notes += notes
        # 收集指纹供跨塔查重（D）
        vjson = src / "version.json"
        if vjson.exists():
            try:
                v = json.loads(vjson.read_text())
                ov_path = REPO_ROOT / str(v.get("overlay_path") or "")
                if ov_path.exists():
                    overlay_shas[name] = _sha(ov_path)
            except (json.JSONDecodeError, OSError):
                pass

    # D：不同塔 overlay 文件不同 → 指纹必须不同
    seen: Dict[str, str] = {}
    for name, sha in overlay_shas.items():
        if sha in seen:
            all_fails.append(
                f"跨塔污染：{seen[sha]} 与 {name} overlay 文件不同但镜像指纹相同 "
                f"（{sha[:12]}…）")
        seen.setdefault(sha, name)

    for n in all_notes:
        print(f"  ⚠ {n}")
    if all_fails:
        print(f"\n✗ 镜像一致性检查失败（{len(all_fails)} 项）：")
        for f in all_fails:
            print(f"  ✗ {f}")
        return 1
    print("✓ 镜像一致性检查通过：web/demo/ 两塔镜像与 out/ 同源（sha 级）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
