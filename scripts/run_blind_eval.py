#!/usr/bin/env python3
"""P4.2 / Phase 8：ZC1 盲测脚手架（discipline-first，无 GT 则显式退出）。

读取 examples/dataset_split.json 的 blind_test 切分：
  * blind_test 为空 → 退出码 2，打印纪律说明（不得把 development 当 blind 汇报）
  * 指定 --run-deliver → 用 production_dxf profile 跑 JC1 对照（仍标注 development）
  * 未来 blind 塔型有 GT 后：--gt + --model 走 eval_a2_profiles（标注 blind_test）

用法：
    python3 scripts/run_blind_eval.py
    python3 scripts/run_blind_eval.py --check-split
    python3 scripts/run_blind_eval.py --run-deliver --agent-mode ezdxf
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPLIT_PATH = REPO / "examples/dataset_split.json"
ZC1_DXF = REPO / "examples/external/guowang_35A1/35C2-SJG1-ML.dxf"


def load_split() -> dict:
    if not SPLIT_PATH.exists():
        return {}
    return json.loads(SPLIT_PATH.read_text(encoding="utf-8"))


def blind_status(split: dict) -> dict:
    bt = (split.get("splits") or {}).get("blind_test") or {}
    datasets = list(bt.get("datasets") or [])
    has_gt = False
    gt_paths: list[str] = []
    for ds in datasets:
        p = REPO / ds if not Path(ds).is_absolute() else Path(ds)
        if p.suffix == ".json" and "ground_truth" in p.name.lower():
            has_gt = p.exists()
            gt_paths.append(str(p))
    return {
        "blind_datasets": datasets,
        "blind_dataset_count": len(datasets),
        "has_blind_gt": has_gt,
        "gt_paths": gt_paths,
        "zc1_dxf_exists": ZC1_DXF.exists(),
        "zc1_has_gt": False,
        "note": bt.get("note") or "",
        "policy": split.get("rules", {}).get("no_same_tower_tuning_report", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 8 blind test scaffold")
    ap.add_argument("--check-split", action="store_true",
                    help="仅检查 dataset_split 盲测切分状态")
    ap.add_argument("--run-deliver", action="store_true",
                    help="跑 production_dxf deliver（development 对照，非 blind 成绩）")
    ap.add_argument("--agent-mode", choices=["ezdxf", "hybrid"], default="ezdxf")
    ap.add_argument("--model", type=Path, default=None,
                    help="已有 model.json（blind GT 可用时评测）")
    ap.add_argument("--gt", type=Path, default=None,
                    help="blind_test GT（未来启用）")
    args = ap.parse_args()

    split = load_split()
    status = blind_status(split)

    print("=== Phase 8 Blind Test Discipline ===")
    print(json.dumps(status, ensure_ascii=False, indent=2))

    if status["blind_dataset_count"] == 0:
        print("\n[BLOCKED] blind_test 切分为空——当前无合法盲测集。")
        print("  JC1 development 指标不得对外宣称 blind_test 成绩（P4.3）。")
        if status["zc1_dxf_exists"]:
            print(f"  ZC1 DXF 存在 ({ZC1_DXF.name}) 但无独立 GT，暂不能盲测。")
        if args.check_split:
            return 2
    else:
        print(f"\nblind_test 数据集: {status['blind_dataset_count']} 项")

    if args.model and args.gt:
        if not args.model.exists() or not args.gt.exists():
            print("model 或 GT 路径不存在", file=sys.stderr)
            return 2
        print("\n=== blind_test eval（有 GT）===")
        rc = subprocess.call([
            sys.executable, str(REPO / "scripts/eval_a2_profiles.py"),
            str(args.gt), str(args.model),
            "--json-out", str(args.model.parent / "eval_blind_a2_profiles.json"),
        ])
        if rc == 0:
            out = args.model.parent / "eval_blind_a2_profiles.json"
            data = json.loads(out.read_text(encoding="utf-8"))
            data["dataset_split"] = "blind_test"
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return rc

    if args.run_deliver:
        print("\n=== production_dxf deliver（development 对照，非 blind 汇报）===")
        return subprocess.call([
            sys.executable, str(REPO / "scripts/run_35A1_jc1_full.py"),
            "--profile", "production_dxf",
            "--agent-mode", args.agent_mode,
        ])

    if args.check_split or status["blind_dataset_count"] == 0:
        return 2

    print("Provide --model + --gt for blind eval, or --run-deliver for production对照.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
