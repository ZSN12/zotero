"""P2 — MLLM vs rule-based-scan 同图三列对比（评测与定位用，不参与主路径）。

用法：
    python benchmark/mllm_vs_scan.py
    python benchmark/mllm_vs_scan.py --image examples/clear/tower_front_hd.png \
        --out examples/external/mllm_benchmark.json
    python benchmark/mllm_vs_scan.py --skip-mllm   # 只跑 rule-based-scan

对同一张图输出三列对比：
    1. rule-based-scan  候选数（杆件 / 节点 / 有件号杆件）
    2. kimi-for-coding  杆件数 / 件号数
    3. k3-256k          杆件数 / 件号数

MLLM 需要 KIMI_API_KEY（MLLM_PROVIDER=kimi-code）或显式 --api-key；
未配置时如实记录 status=unavailable + failure_reason，绝不编造数字。
结果写入 examples/external/mllm_benchmark.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_IMAGE = REPO / "examples" / "clear" / "tower_front_hd.png"
DEFAULT_OUT = REPO / "examples" / "external" / "mllm_benchmark.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_labeled_bar_id(bid: str) -> bool:
    """有真实件号才算 labeled；UNLABELED / SCAN_ 是占位或扫描自编号。"""
    if not bid:
        return False
    return not bid.upper().startswith(("UNLABELED", "SCAN_"))


def count_scan_model(model) -> Dict[str, Any]:
    """统计 EngineeringModel（rule-based-scan 输出）的候选数。"""
    bars = [c for c in model.components.values() if c.kind == "tower_bar"]
    nodes = [c for c in model.components.values() if c.kind == "tower_node"]
    regions = [c for c in model.components.values() if c.kind == "scan_region"]
    labeled = 0
    for b in bars:
        bid = str(b.properties.get("bar_id", ""))
        if _is_labeled_bar_id(bid):
            labeled += 1
    return {
        "bars": len(bars),
        "nodes": len(nodes),
        "regions": len(regions),
        "labeled_bars": labeled,
    }


def count_candidate(candidate) -> Dict[str, Any]:
    """统计 ModelCandidate（MLLM 输出）的杆件/节点/件号数。"""
    bars = [o for o in candidate.objects
            if o.obj_type == "component" and o.data.get("kind") == "tower_bar"]
    nodes = [o for o in candidate.objects
             if o.obj_type == "component" and o.data.get("kind") == "tower_node"]
    views = [o for o in candidate.objects
             if o.obj_type == "component" and o.data.get("kind") == "drawing_view"]
    labeled = 0
    for b in bars:
        bid = str((b.data.get("properties") or {}).get("bar_id", ""))
        if _is_labeled_bar_id(bid):
            labeled += 1
    return {
        "bars": len(bars),
        "nodes": len(nodes),
        "views": len(views),
        "labeled_bars": labeled,
    }


def run_rule_based_scan(image: Path) -> Dict[str, Any]:
    """rule-based-scan：塔形版面分析 + 霍夫线检测，不依赖 MLLM API。"""
    from traceability.intake.mllm_backend import DrawingInput, TowerScanBackend

    drawing = DrawingInput(path=str(image), kind="scan", tower=True)
    t0 = time.time()
    candidate = TowerScanBackend().analyze(drawing)
    row: Dict[str, Any] = {
        "backend": "rule-based-scan",
        "model": None,
        "status": "ok" if candidate.objects else "empty",
        "elapsed_s": round(time.time() - t0, 2),
    }
    row.update(count_candidate(candidate))
    return row


def run_mllm(
    image: Path,
    model: str,
    api_key: Optional[str] = None,
    provider: str = "kimi-code",
) -> Dict[str, Any]:
    """调用 MLLM 后端（kimi-for-coding / k3-256k）。"""
    from traceability.intake.mllm_backend import DrawingInput, MLLMBackend

    backend = MLLMBackend(provider=provider, model=model, api_key=api_key)
    drawing = DrawingInput(path=str(image), kind="scan", tower=True)
    t0 = time.time()
    candidate = backend.analyze(drawing)
    elapsed = round(time.time() - t0, 2)

    row: Dict[str, Any] = {
        "backend": "mllm",
        "model": model,
        "provider": provider,
        "status": "ok" if candidate.objects else "empty",
        "elapsed_s": elapsed,
        "meta": candidate.meta,
    }
    if candidate.objects:
        row.update(count_candidate(candidate))
        row["parse_warnings"] = len(candidate.warnings)
    else:
        # 0 产出时件号/杆件数记为 null（未产出 ≠ 0 根），避免误导
        row.update({"bars": None, "nodes": None, "views": None,
                    "labeled_bars": None})
        row["failure_reason"] = (candidate.meta or {}).get("failure_reason") or str(candidate.raw)
        if candidate.raw and str(candidate.raw).startswith("未配置"):
            row["status"] = "unavailable"
    return row


def run_benchmark(
    image: Path = DEFAULT_IMAGE,
    out: Path = DEFAULT_OUT,
    api_key: Optional[str] = None,
    skip_mllm: bool = False,
    mllm_models: Optional[List[str]] = None,
    mllm_runner: Optional[Callable[[Path, str, Optional[str]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """跑三列对比并写 JSON。

    mllm_runner 可注入 mock（测试用），签名同 run_mllm 的后两个参数。
    """
    image = Path(image)
    out = Path(out)
    runner = mllm_runner or run_mllm

    image_display = str(image)
    try:
        image_display = str(image.relative_to(REPO))
    except ValueError:
        pass

    result: Dict[str, Any] = {
        "image": image_display,
        "generated_at": _iso_now(),
        "note": "评测定位用，不与矢量主路径争抢。MLLM 未配置 API 时如实记录 unavailable。",
        "columns": [],
    }

    result["columns"].append(run_rule_based_scan(image))
    if not skip_mllm:
        for model in (mllm_models or ["kimi-for-coding", "k3-256k"]):
            result["columns"].append(runner(image, model, api_key))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _print_table(result: Dict[str, Any]) -> None:
    print(f"image: {result['image']}")
    print(f"out:   {result.get('_out')}")
    header = ("列", "backend/model", "杆件 bars", "节点 nodes", "件号 labeled", "状态")
    rows = []
    for col in result["columns"]:
        name = col["backend"] if not col.get("model") else f"{col['backend']}/{col['model']}"
        def _cell(value):
            return "-" if value is None else str(value)

        rows.append((
            col["backend"],
            name,
            _cell(col.get("bars")),
            _cell(col.get("nodes")),
            _cell(col.get("labeled_bars")),
            col.get("status", "?"),
        ))
    widths = [max(len(r[i]) for r in rows + [header]) for i in range(6)]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(header)))
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(6)))
    for col in result["columns"]:
        if col.get("status") in ("unavailable", "empty") and col.get("failure_reason"):
            print(f"  ⚠ {col['backend']}/{col.get('model')}: {col['failure_reason']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MLLM vs rule-based-scan 三列对比（P2）")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE),
                        help="输入图（默认 examples/clear/tower_front_hd.png）")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="输出 JSON（默认 examples/external/mllm_benchmark.json）")
    parser.add_argument("--api-key", default=None,
                        help="Kimi API Key（默认读 KIMI_API_KEY/OPENAI_API_KEY）")
    parser.add_argument("--skip-mllm", action="store_true",
                        help="只跑 rule-based-scan，不调 MLLM")
    args = parser.parse_args(argv)

    result = run_benchmark(
        image=Path(args.image),
        out=Path(args.out),
        api_key=args.api_key,
        skip_mllm=args.skip_mllm,
    )
    result["_out"] = str(Path(args.out))
    _print_table(result)
    print(f"\n✓ 已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
