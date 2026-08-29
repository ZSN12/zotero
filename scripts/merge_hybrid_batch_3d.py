#!/usr/bin/env python3
"""将 JC1 hybrid Kimi 批跑产物合并为可查看的 3D GLB（薄包装）。

⚠️ DEPRECATED（P1 入口收口）：本脚本仅为兼容旧命令，业务逻辑已全部迁入
正式 API（deliver_project 产出 skeleton.glb / canonical.glb，export_detail_qa_atlas
产出 detail_qa_atlas.glb）。此处只转发参数，不再保留任何独立 pipeline 实现。

等价主路径：
    python3 -m traceability.cli deliver-project <batch-dir> --agent-mode hybrid

示例：
    python3 scripts/merge_hybrid_batch_3d.py \\
      --batch-dir out/jc1-hybrid-kimi-batch \\
      --layer-map examples/external/guowang_35A1/layer_overlay.json
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="hybrid 批跑 → 3D GLB（薄包装）")
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--layer-map", type=Path, required=True)
    parser.add_argument("--bom", type=Path, default=None)
    parser.add_argument("--allow-derived-y", action="store_true")
    args = parser.parse_args()

    from traceability.cli import main as cli_main

    argv = [
        "deliver-project", str(args.batch_dir),
        "--out-dir", str(args.batch_dir),
        "--agent-mode", "hybrid",
        "--layer-map", str(args.layer_map),
    ]
    if args.bom:
        argv += ["--bom", str(args.bom)]

    try:
        cli_main(argv)
    except SystemExit as exc:
        return exc.code or 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
