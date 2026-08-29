#!/usr/bin/env python3
"""JC1 全册 DXF hybrid 批跑（薄包装）。

⚠️ DEPRECATED（P1 入口收口）：全册批跑请改用统一主路径
    python3 scripts/run_35A1_jc1_full.py --agent-mode hybrid
  或
    python3 -m traceability.cli deliver-project <dir> --agent-mode hybrid
本脚本仅为兼容旧命令，业务逻辑已全部迁入 deliver_project，此处只转发参数，
不再保留任何独立 pipeline 实现。

示例：
    MLLM_PROVIDER=kimi-code MLLM_MODEL=k3-256k \\
    python3 scripts/run_jc1_hybrid_batch.py \\
      --dxf-dir out/xianyu-acceptance/batch-jc1/dxf \\
      --out-dir out/jc1-hybrid-kimi-batch \\
      --layer-map examples/external/guowang_35A1/layer_overlay.json
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="JC1 全册 hybrid 批跑（薄包装）")
    parser.add_argument("--dxf-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--layer-map", type=Path, default=None)
    parser.add_argument("--dpi", type=int, default=400)
    parser.add_argument("--geom-method", default="auto")
    parser.add_argument("--mllm-provider", default=None)
    parser.add_argument("--mllm-model", default=None)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--post-3d", action="store_true")
    args = parser.parse_args()

    from traceability.cli import main as cli_main

    argv = [
        "deliver-project", str(args.dxf_dir),
        "--out-dir", str(args.out_dir),
        "--agent-mode", "hybrid",
    ]
    if args.layer_map:
        argv += ["--layer-map", str(args.layer_map)]

    try:
        cli_main(argv)
    except SystemExit as exc:
        return exc.code or 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
