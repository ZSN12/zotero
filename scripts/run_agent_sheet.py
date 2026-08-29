#!/usr/bin/env python3
"""单张 DXF hybrid Agent 跑批（薄包装）。

⚠️ DEPRECATED（P1 入口收口）：本脚本仅为兼容旧命令，业务逻辑已全部迁入
正式 API `run-tower --agent-mode hybrid`，此处只是转发参数，不再保留任何
独立 pipeline 实现。

等价主路径：
    python3 -m traceability.cli run-tower <dxf> --agent-mode hybrid \\
        --layer-map <overlay> [--bom <bom.csv>]

示例：
    python3 scripts/run_agent_sheet.py \\
      out/xianyu-acceptance/batch-jc1/dxf/35A1-JC1-02.dxf \\
      --out-dir out/jc1-02-hybrid \\
      --layer-map examples/external/guowang_35A1/layer_overlay.json
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    from traceability.cli import main as cli_main

    # 解析原参数，转译为 run-tower --agent-mode hybrid 的 argv。
    import argparse
    parser = argparse.ArgumentParser(description="DXF hybrid 单张跑批（薄包装）")
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--layer-map", type=Path, default=None)
    parser.add_argument("--bom", type=Path, default=None)
    parser.add_argument("--dpi", type=int, default=400)
    parser.add_argument("--mllm-provider", default=None)
    parser.add_argument("--mllm-model", default=None)
    parser.add_argument("--no-ocr-fallback", action="store_true")
    parser.add_argument("--geom-method", default="auto")
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()

    argv = [
        "run-tower", str(args.dxf),
        "--out-dir", str(args.out_dir),
        "--agent-mode", "hybrid",
    ]
    if args.layer_map:
        argv += ["--layer-map", str(args.layer_map)]
    if args.bom:
        argv += ["--bom", str(args.bom)]
    if args.no_ocr_fallback:
        argv.append("--no-ocr-fallback")
    if args.merge:
        argv.append("--merge")

    try:
        cli_main(argv)
    except SystemExit as exc:
        return exc.code or 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
