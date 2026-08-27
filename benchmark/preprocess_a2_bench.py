"""Phase C — 预处理前后 A2 霍夫基准评测。

量化 preprocess_for_scan 对 bars/节点召回与噪声的影响：
    * raw：原始灰度图直接霍夫
    * preprocessed：线重绘预处理后再霍夫

用法：
    python benchmark/preprocess_a2_bench.py
    python benchmark/preprocess_a2_bench.py --image path/to/scan.png --out bench.json
    python benchmark/preprocess_a2_bench.py --synthetic   # 无样例图时用合成图
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_OUT = REPO / "examples" / "external" / "preprocess_a2_bench.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _detect_counts(image_path: str, preprocess: bool) -> Dict[str, Any]:
    from traceability.intake.tower_agent_pipeline import _detect_geometry
    from traceability.intake.tower_preprocess import preprocess_image_file

    t0 = time.time()
    path = image_path
    pre_meta = None
    if preprocess:
        path, pre_meta = preprocess_image_file(image_path)
    bars, nodes, geom_meta = _detect_geometry(path, filter_noise=True, use_preprocess=False)
    row = {
        "bars": len(bars),
        "nodes": len(nodes),
        "elapsed_s": round(time.time() - t0, 3),
        **geom_meta,
    }
    if pre_meta:
        row["preprocess"] = pre_meta
    return row


def _make_synthetic_png(out_dir: Path) -> Path:
    """合成十字网格图（弱线 + 噪点），无外部样例时可跑基准。"""
    import numpy as np

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("合成图需要 opencv-python") from exc

    h, w = 800, 1200
    img = np.full((h, w), 240, dtype="uint8")
    # 主结构线（略浅，模拟弱中心线）— 线宽 2px 保证霍夫可检
    for x in range(100, w - 100, 80):
        cv2.line(img, (x, 80), (x, h - 80), 120, 2)
    for y in range(100, h - 100, 60):
        cv2.line(img, (80, y), (w - 80, y), 120, 2)
    # 随机噪点
    rng = np.random.default_rng(42)
    for _ in range(400):
        cx, cy = int(rng.integers(0, w)), int(rng.integers(0, h))
        cv2.circle(img, (cx, cy), 1, 120, -1)
    path = out_dir / "synthetic_scan.png"
    cv2.imwrite(str(path), img)
    return path


def run_bench(image: Path, out: Path, synthetic: bool = False) -> Dict[str, Any]:
    import tempfile

    tmp_ctx = None
    if synthetic or not image.exists():
        tmp_ctx = tempfile.TemporaryDirectory()
        image = _make_synthetic_png(Path(tmp_ctx.name))

    try:
        raw = _detect_counts(str(image), preprocess=False)
        pre = _detect_counts(str(image), preprocess=True)

        report = {
            "generated_at": _iso_now(),
            "image": str(image),
            "raw_hough": raw,
            "preprocessed_hough": pre,
            "delta": {
                "bars": pre["bars"] - raw["bars"],
                "nodes": pre["nodes"] - raw["nodes"],
                "noise_removed_delta": pre.get("noise_removed", 0) - raw.get("noise_removed", 0),
            },
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def main():
    p = argparse.ArgumentParser(description="Phase C preprocess vs raw Hough benchmark")
    p.add_argument("--image", type=Path,
                   default=REPO / "examples" / "clear" / "tower_front_hd.png")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--synthetic", action="store_true", help="无样例图时用合成扫描图")
    args = p.parse_args()
    report = run_bench(args.image, args.out, synthetic=args.synthetic or not args.image.exists())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
