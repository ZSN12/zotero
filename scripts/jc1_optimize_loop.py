#!/usr/bin/env python3
"""35A1-JC1 hybrid 交付的自动化优化循环。

用法：
    python3 scripts/jc1_optimize_loop.py [--max-iter 5] [--wait-quota]

职责：
    1. 轮询 MLLM 配额（403 usage limit 时等待，恢复后继续）。
    2. 跑 deliver_project --agent-mode hybrid（unbuffered，异常落盘）。
    3. 跑 evaluate_ground_truth + gt_segment_recall，追加到迭代日志。
    4. 每轮结果写入 out/35A1-JC1-full-deliver/iteration_log.md。

这是「持续优化直到 GT 达标」的执行入口。单轮约 60-90 分钟（50 张图 MLLM）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "out" / "35A1-JC1-full-deliver"
GT = ROOT / "examples" / "gt" / "35A1-JC1_ground_truth.json"
LOG = OUT / "iteration_log.md"


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mllm_quota_ok() -> tuple[bool, str]:
    """返回 (是否可用, 说明)。做一次最小调用探测 403 配额。"""
    from traceability.intake.mllm_backend import MLLMBackend
    m = MLLMBackend()
    if not m.available():
        return False, "无 API key"
    try:
        _, meta = m.call_agent_json('输出 JSON: {"ok": true}', None, None, agent="quota_probe")
    except Exception as exc:  # noqa: BLE001
        return False, f"调用异常: {type(exc).__name__}"
    reason = meta.get("failure_reason") or ""
    if "403" in reason or "usage limit" in reason.lower() or "quota" in reason.lower():
        return False, f"配额耗尽: {reason[:120]}"
    if meta.get("elapsed_s") is None and not reason:
        return True, "探测通过"
    return True, "探测通过" if not reason else f"ok ({reason[:80]})"


def _run(cmd: list[str], out_log: Path) -> int:
    print(f"  $ {' '.join(cmd)}", flush=True)
    with open(out_log, "w", encoding="utf-8") as fh:
        fh.write(f"# {_ts()} $ {' '.join(cmd)}\n")
        fh.flush()
        proc = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def _append_log(text: str) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(text)


def run_one_iteration(iter_no: int) -> None:
    print(f"\n=== 迭代 {iter_no} @ {_ts()} ===", flush=True)
    _append_log(f"\n## 迭代 {iter_no} @ {_ts()}\n")

    # 1. 交付（unbuffered）
    run_log = OUT / f"hybrid_iter{iter_no}.log"
    rc = _run([
        sys.executable, "-u", "scripts/run_35A1_jc1_full.py", "--agent-mode", "hybrid",
    ], run_log)
    if rc != 0:
        _append_log(f"- ❌ 交付退出码 {rc}，见 {run_log.name}\n")
        print(f"  交付退出码 {rc}", flush=True)
    else:
        _append_log("- ✅ 交付完成\n")

    # 2. 评测
    model_path = OUT / "model.json"
    if not model_path.exists():
        _append_log("- ❌ 无 model.json，跳过评测\n")
        return
    eval_cmd = [
        sys.executable, "scripts/evaluate_ground_truth.py", str(GT), str(model_path),
    ]
    proc = subprocess.run(eval_cmd, cwd=ROOT, capture_output=True, text=True)
    _append_log(f"```\n{proc.stdout}\n```\n")

    # 3. 分段召回
    seg_cmd = [
        sys.executable, "scripts/gt_segment_recall.py", str(GT), str(model_path),
    ]
    proc2 = subprocess.run(seg_cmd, cwd=ROOT, capture_output=True, text=True)
    _append_log(f"```\n{proc2.stdout}\n```\n")

    # 4. 备份本轮产物（避免下一轮覆盖历史基线）
    snap = OUT / "iter_snapshots" / f"iter{iter_no}"
    snap.mkdir(parents=True, exist_ok=True)
    for name in ("model.json", "assembly.glb", "canonical.glb", "full_run_report.json"):
        src = OUT / name
        if src.exists():
            import shutil
            shutil.copy2(src, snap / name)
    _append_log(f"- 📦 本轮产物快照: {snap.relative_to(ROOT)}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-iter", type=int, default=5)
    ap.add_argument("--wait-quota", action="store_true",
                    help="MLLM 配额耗尽时轮询等待（而非直接退出）")
    ap.add_argument("--poll-sec", type=int, default=600, help="配额轮询间隔秒")
    args = ap.parse_args()

    # 等待配额不消耗迭代次数：用独立 while 循环等到配额可用，
    # 只有真正跑迭代才递增 iter_no（否则 --wait-quota 会因 5 次轮询耗尽
    # max_iter 而提前退出，一次迭代都不跑）。
    for i in range(1, args.max_iter + 1):
        while True:
            ok, why = _mllm_quota_ok()
            if ok:
                break
            if not args.wait_quota:
                print(f"❌ MLLM 不可用（{why}），退出。可用 --wait-quota 轮询等待。", flush=True)
                return 1
            print(f"⏳ MLLM 不可用（{why}），{args.poll_sec}s 后重试...", flush=True)
            _append_log(f"- ⏳ MLLM 不可用（{why}），等待配额恢复\n")
            time.sleep(args.poll_sec)
        run_one_iteration(i)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
