#!/usr/bin/env python3
"""初始化一座新塔的工作区（init_domain）。

开源基座对标（2026-09-03）：换一座塔的成本从「读懂整套代码」降为
「填一份 overlay.json + 放图纸 + 放 BOM」。SKILL.md 第 3 节的多塔
泛化纪律（只改配置，不改代码）落到一个可执行的脚手架上：

  python3 init_domain.py <workspace_dir> --name <tower-id> [--gt]

生成结构：
  <workspace>/
    overlay.json    # 多塔配置模板（view_regions / z-only 层表 /
                    #   跨度白名单……字段带 _doc 说明）
    dxf/            # 放本塔图纸（*.dxf / *.dwg）
    bom/bom.csv     # BOM 模板（表头 + 一行示例）
    gt/             # --gt 时生成 ground_truth.json 骨架（含 caveats 纪律）
    README.md       # 六层入口 + 两道门禁的 canonical 命令清单
    out/            # 管线产物（建议 gitignore）

模板里的 GT 注入键默认只给 z-only 形态（SKILL.md 铁律 1：
严禁注入 x/y 坐标或拓扑）。validate_workspace.py 会在跑批前把关。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

_OVERLAY_TEMPLATE = {
    "_doc": [
        "angle-tower 工作区配置。字段说明见 domains/angle-tower/SKILL.md 第 3 节。",
        "铁律 1：GT 注入只许 z-only（层表/跨度表）；严禁 x/y 坐标或拓扑注入。",
        "多册塔必须声明 view_regions（每册立面 z 域，从图纸实测，不猜）。",
    ],
    "name": "<tower-id>",
    "view_regions": {
        "<sheet-stem>": {"z_lo": 0.0, "z_hi": 0.0, "view": "front"},
    },
    "_doc_view_regions": "键 = DXF 文件 stem；单册塔可删除本节（走 run_tower 路径）。",
    "cross_file_views": {
        "_doc": "多册空间合并开关 + 参与合并的册（stems）；单册塔删除本节。",
        "parse_all_project_sheets": True,
        "stems": ["<sheet-stem>"],
    },
    "enable_4_face_expansion": True,
    "side_read_promotion": True,
    "_doc_gt_z_only": (
        "z-only 设计常数（可选，按本塔实测填写；version.json 自动登记"
        " gt_injected.surfaces）："),
    "gt_platform_levels_override": [],
    "gt_terminal_levels_override": [],
    "gt_diaphragm_levels_override": [],
    "terminal_pair_span_whitelist": [],
    "_doc_terminal_span": "终止节间跨度白名单：[[z_lo, z_hi], ...]，从本塔长斜杆端点对聚类推导（≥2 refs）。",
    "diagonal_topology_sheets": ["<sheet-stem>"],
    "disable_scale_calibration": False,
    "_doc_disable_calib": "true 时 DIM 样本仍登记为观测，但不参与 region scale 覆盖（JC2 系噪声防护）。",
}

_BOM_TEMPLATE = (
    "bar_id,section,length_mm,qty\n"
    "0001,L40X4,1000,1\n"
)

_GT_TEMPLATE = {
    "_doc": [
        "本塔 GT（可选）。来源等级决定可否对外并列呈报：",
        "  mod_direct —— .mod/.NODE 直出，可并列呈报；",
        "  glb_reextract —— GLB 反提取，仅限内部回归（必须写 caveats）。",
        "keys: bars[]（3D 端点 + id + section）、caveats[]（来源与限制说明）。",
    ],
    "source": "<mod_direct|glb_reextract|manual>",
    "bars": [],
    "caveats": [],
}

_README_TEMPLATE = """# {name} 工作区（angle-tower 领域包）

六层契约工作区：把图纸放进 `dxf/`、BOM 放进 `bom/bom.csv`、
按图纸实测填 `overlay.json`（字段说明在文件内 `_doc` 键）。

## 流程

```bash
# 0. 预检（配置纪律：z-only 注入面 / BOM member 行 / 册-区域一致性）
python3 domains/angle-tower/scripts/validate_workspace.py {ws}

# 1~6. 六层逐层可跑/可审计（首层自动跑管线，后续复用产物）
for L in 1 2 3 4 5 6; do
  python3 domains/angle-tower/scripts/run_layer.py $L --workspace {ws}
done

# 门禁（可选产物：--out-dir {ws}/out）
python3 domains/angle-tower/scripts/validate_public_ir.py {ws}/out/model.json
```

## 纪律（违反 = 交付无效）

1. GT 注入只许 z-only（层表/跨度表）；严禁 x/y。
2. 对外只报 A2-dual-view-pure；reconstructed/level_assisted 分层呈报。
3. 每物必有源（SourceRef）；改动沿 DAG 传播 staleness。
详见 `domains/angle-tower/SKILL.md`。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("workspace", type=Path, help="工作区目录（不存在则创建）")
    ap.add_argument("--name", required=True, help="塔号（如 35A2-JC3）")
    ap.add_argument("--gt", action="store_true", help="同时生成 GT 骨架")
    args = ap.parse_args()

    ws = args.workspace.resolve()
    if ws.exists() and any(ws.iterdir()):
        print(f"目录非空，拒绝覆盖：{ws}", file=sys.stderr)
        return 1

    for sub in ("dxf", "bom", "out"):
        (ws / sub).mkdir(parents=True, exist_ok=True)

    overlay = json.loads(json.dumps(_OVERLAY_TEMPLATE))
    overlay["name"] = args.name
    (ws / "overlay.json").write_text(
        json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
    (ws / "bom" / "bom.csv").write_text(_BOM_TEMPLATE, encoding="utf-8")
    if args.gt:
        (ws / "gt").mkdir(exist_ok=True)
        (ws / "gt" / "ground_truth.json").write_text(
            json.dumps(_GT_TEMPLATE, ensure_ascii=False, indent=2),
            encoding="utf-8")
    (ws / "README.md").write_text(
        _README_TEMPLATE.format(name=args.name, ws=ws), encoding="utf-8")
    (ws / "out" / ".gitignore").write_text("*\n", encoding="utf-8")

    print(f"已初始化工作区：{ws}")
    print("下一步：")
    print(f"  1. 把图纸（*.dxf/*.dwg）放进 {ws / 'dxf'}")
    print(f"  2. 用真实 BOM 覆盖 {ws / 'bom' / 'bom.csv'}")
    print(f"  3. 按图纸实测填写 {ws / 'overlay.json'}（字段见 _doc 键）")
    print(f"  4. 预检：python3 domains/angle-tower/scripts/validate_workspace.py {ws}")
    print(f"  5. 六层：for L in 1 2 3 4 5 6; do python3 domains/angle-tower/scripts/run_layer.py $L --workspace {ws}; done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
