#!/usr/bin/env python3
"""对比两轮迭代的 GT 评测结果，生成增量报告。

用法：
    python3 scripts/compare_iters.py <gt.json> <model_prev.json> <model_curr.json> [--tol 500]

用于优化循环每轮跑完后，快速判断「上一轮 → 本轮」的 Precision/Recall/
分段召回变化，避免人工逐行读 iteration_log.md。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run_eval(gt: str, model: str, tol: int) -> str:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/evaluate_ground_truth.py"), gt, model, "--tol", str(tol)],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.stdout


def parse_metrics(text: str) -> dict:
    """从评测输出里抽 Precision/Recall/TP/FP/FN/件号。"""
    m = {}
    for line in text.splitlines():
        line = line.strip()
        if "Precision" in line and "%" in line:
            try:
                m["precision"] = float(line.split(":")[-1].strip().rstrip("%"))
            except ValueError:
                pass
        elif "Recall" in line and "%" in line:
            try:
                m["recall"] = float(line.split(":")[-1].strip().rstrip("%"))
            except ValueError:
                pass
        elif "匹配 (TP)" in line:
            try:
                m["tp"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "误报 (FP)" in line:
            # 形如 "误报 (FP): 211 | 漏检 (FN): 240"
            try:
                m["fp"] = int(line.split("误报 (FP):")[-1].split("|")[0].strip())
            except ValueError:
                pass
            if "漏检 (FN)" in line:
                try:
                    m["fn"] = int(line.split("漏检 (FN):")[-1].strip())
                except ValueError:
                    pass
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gt")
    ap.add_argument("prev")
    ap.add_argument("curr")
    ap.add_argument("--tol", type=int, default=500)
    args = ap.parse_args()

    prev_txt = run_eval(args.gt, args.prev, args.tol)
    curr_txt = run_eval(args.gt, args.curr, args.tol)
    prev_m = parse_metrics(prev_txt)
    curr_m = parse_metrics(curr_txt)

    print("=== 迭代对比 ===")
    print(f"{'指标':<12}{'上一轮':>12}{'本轮':>12}{'变化':>12}")
    for key, label in (("precision", "Precision%"), ("recall", "Recall%"),
                       ("tp", "TP"), ("fp", "FP"), ("fn", "FN")):
        p = prev_m.get(key)
        c = curr_m.get(key)
        if p is None or c is None:
            continue
        if key in ("precision", "recall"):
            delta = c - p
            print(f"{label:<12}{p:>11.1f}%{c:>11.1f}%{delta:>+11.1f}")
        else:
            delta = c - p
            print(f"{label:<12}{p:>12}{c:>12}{delta:>+12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
