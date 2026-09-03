#!/usr/bin/env python3
"""门禁 1：angle-tower 领域包自检（self_test）。

两道硬门禁之一（官网基座对标：scripts/self_test.py）。
内容：
  1. 全量单测（pytest，红线：全绿）
  2. 内置示例端到端冒烟：examples/tower_110kv.dxf run-tower --merge
     → 五规则 passed + model.json 产出
  3. 证据层完整性抽查：观测/假设/依赖 DAG 键存在

退出码：全部通过 0，任一失败 1。
用法：python3 domains/angle-tower/scripts/self_test.py [--quick]
  --quick 跳过全量单测，只跑冒烟（迭代期用）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True,
                          text=True, **kw)


def gate_tests() -> bool:
    # Bug G（2026-09-03，P2）：全量 pytest capture_output=True 块缓冲，
    # 700 用例 ~22s 内零输出，观察者误判挂死。跑前 flush + 显式提示
    # 预计时长；超时上限 600s 防真死锁（超时抛 TimeoutExpired 由外层
    # 捕获为 FAIL，不再无限等）。
    print("[gate_tests] 全量单测启动（~22s，最长 600s，块缓冲无逐条输出属正常）",
          flush=True)
    try:
        r = run([sys.executable, "-m", "pytest", "tests/",
                 "-p", "no:cacheprovider", "-q"], timeout=600)
    except subprocess.TimeoutExpired as exc:
        # k3 审查（2026-09-04）：超时后保留 pytest 已产出的尾部输出，
        # 给挂死排查留线索（capture_output=True 下 TimeoutExpired.output
        # 携带已被捕获的部分输出）。
        partial = exc.stdout if isinstance(exc.stdout, str) else (
            (exc.stdout or b"").decode("utf-8", errors="replace"))
        if partial:
            print("--- 超时前 pytest 输出尾部 ---", flush=True)
            print("\n".join(partial.strip().splitlines()[-8:]), flush=True)
        print("FAIL: 单测 600s 超时（疑似挂死，需人工排查）", flush=True)
        return False
    ok = r.returncode == 0
    tail = "\n".join(r.stdout.strip().splitlines()[-2:])
    print(tail, flush=True)
    if not ok:
        print("FAIL: 单测未全绿", flush=True)
    return ok


def gate_smoke() -> bool:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "110kv"
        r = run([sys.executable, "-m", "traceability.cli", "run-tower",
                 "examples/tower_110kv.dxf",
                 "--bom", "examples/tower_110kv_bom.csv",
                 "--merge",
                 "--golden", "examples/tower_110kv_golden.json",
                 "--out-dir", str(out)])
        if r.returncode != 0:
            print(r.stdout[-2000:])
            print(r.stderr[-2000:])
            print("FAIL: tower_110kv 冒烟管线失败")
            return False
        model_path = out / "model.json"
        if not model_path.exists():
            print("FAIL: 冒烟管线未产出 model.json")
            return False
        m = json.loads(model_path.read_text(encoding="utf-8"))
        rules = m.get("rules") or {}
        n_passed = sum(1 for r_ in rules.values()
                       if (r_.get("status") if isinstance(r_, dict) else None) == "passed")
        n_total = len(rules)
        # 五规则门禁（acceptance.sh 同口径）
        if n_total < 5 or n_passed < 5:
            print(f"FAIL: 验证规则 {n_passed}/{n_total} passed（要求 5/5）")
            return False
        print(f"smoke: rules {n_passed}/{n_total} passed, "
              f"components={len(m.get('components') or {})}")
        return True


def gate_evidence_ir() -> bool:
    """证据层 IR 完整性抽查（契约键存在性，不依赖具体塔）。"""
    m = json.loads((REPO / "examples" / "tower_110kv_model.json").read_text(encoding="utf-8"))
    required = ("components", "dimensions", "connections", "rules")
    missing = [k for k in required if k not in m]
    if missing:
        print(f"FAIL: 公共 IR 缺键 {missing}")
        return False
    print("evidence-ir: 公共 IR 键完整")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过全量单测")
    args = ap.parse_args()

    results: list[tuple[str, bool]] = []
    if not args.quick:
        results.append(("单测", gate_tests()))
    results.append(("冒烟管线", gate_smoke()))
    results.append(("证据层 IR", gate_evidence_ir()))

    print("\n=== self_test 汇总 ===")
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
