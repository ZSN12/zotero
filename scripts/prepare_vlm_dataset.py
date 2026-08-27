#!/usr/bin/env python3
"""Phase F — 领域 VLM 微调数据准备脚本（长期项）。

从 ProjectModel / 扫描候选导出 (image, label_json, source_ref) 三元组，
供后续 VLM 微调使用。不参与主链路 runtime。

用法：
    python scripts/prepare_vlm_dataset.py --project-dir examples/external/guowang_35A1 \
        --layer-map examples/external/guowang_35A1/layer_overlay.json \
        --out-dir out/vlm_dataset
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main():
    p = argparse.ArgumentParser(description="Prepare VLM fine-tune dataset from project sheets")
    p.add_argument("--project-dir", type=Path, required=True)
    p.add_argument("--layer-map", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=REPO / "out" / "vlm_dataset")
    args = p.parse_args()

    from traceability.project.model import build_project_from_directory

    args.out_dir.mkdir(parents=True, exist_ok=True)
    project = build_project_from_directory(
        args.project_dir,
        project_id=args.project_dir.name,
        layer_map_path=str(args.layer_map) if args.layer_map else None,
        out_dir=args.out_dir / "models",
    )
    save_project(project, args.out_dir / "project.json")

    manifest = []
    for sid, sheet in project.sheets.items():
        if not sheet.model_path:
            continue
        from traceability.io import load_model
        model = load_model(sheet.model_path)
        labels = []
        for cid, comp in model.components.items():
            if comp.kind not in ("tower_bar", "tower_node"):
                continue
            labels.append({
                "id": cid,
                "kind": comp.kind,
                "properties": comp.properties,
                "source": {
                    "reference": comp.source.reference if comp.source else sheet.path,
                    "detail": comp.source.detail if comp.source else None,
                    "confidence": comp.source.confidence if comp.source else 0.5,
                },
            })
        entry = {
            "sheet_id": sid,
            "source_path": sheet.path,
            "model_path": sheet.model_path,
            "label_count": len(labels),
            "labels": labels[:500],
        }
        manifest.append(entry)
        (args.out_dir / f"{sid}_labels.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    (args.out_dir / "manifest.json").write_text(
        json.dumps({"project": project.project_id, "sheets": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✓ VLM 数据集 manifest -> {args.out_dir / 'manifest.json'} ({len(manifest)} sheets)")


def save_project(project, path):
    from traceability.project.model import save_project as _save
    _save(project, path)


if __name__ == "__main__":
    main()
