#!/usr/bin/env python3
"""Phase 2e 验收跑批：剥离意图声明的 overlay 副本端到端跑 JC1/ZC1。

用法（run_full 传入 -- 前缀的 run 脚本参数）：
    python scripts/run_phase2e_stripped.py --tower jc1 [-- passthru args]

逻辑：临时把 scripts.run_35A1_jc1_full / run_35A2_zc1_full 的
OVERLAY_PATH 指到 out/phase2e/layer_overlay_<t>_stripped.json（view_regions
剥离 kind/axes，保留全部几何/标定/z_offset），输出独立目录
out/phase2e/<tower>-deliver。意图由 sheet_intent 四分类 + intent_router
在管线入口（deliver_project → build_project_from_directory）注册补挂。

红线比对（与 committed overlay 基线）：
    JC1: A2-front-full TP=913 / dual-recon 1067 / dual-pure 304 / A1 168/197
    ZC1: A2-front-full TP=223 / dual-recon 258 / dual-pure 9 / A1 190/202
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TOWERS = {
    "jc1": {
        "script": "scripts/run_35A1_jc1_full.py",
        "src_overlay": REPO / "examples/external/guowang_35A1/layer_overlay.json",
        "out_dir": ["--out-dir", str(REPO / "out/phase2e/jc1-deliver")],
    },
    "zc1": {
        "script": "scripts/run_35A2_zc1_full.py",
        "src_overlay": REPO / "examples/external/guowang_35A2_zc1/layer_overlay.json",
        "out_dir": ["--out-dir", str(REPO / "out/phase2e/zc1-deliver")],
    },
}


def make_stripped(src: Path) -> Path:
    """剥离 view_regions 的 kind+axes（意图声明），写在 overlay 同目录。

    必须同目录：overlay 内的相对引用（crossarm_headless_bom=
    full_bom.json、master_bom 等）按 overlay 同目录解析，搬到 out/ 会
    断 BOM 查找（ZC1 实测 crossarm tip_source 从 bom 退化 hw_fallback，
    A2-front-full TP 223→199）。文件名带 phase2e 前缀，不污染共享 overlay。
    """
    ov = json.loads(src.read_text(encoding="utf-8"))
    n = 0
    for regs in (ov.get("view_regions") or {}).values():
        for r in regs or []:
            k = r.pop("kind", None)
            a = r.pop("axes", None)
            if k is not None or a is not None:
                n += 1
    ov["_doc_phase2e"] = (
        "Phase 2e 验收副本：view_regions 剥离 kind+axes（意图声明），"
        "保留全部几何/标定/z_offset。意图由 sheet_intent 四分类 + "
        "intent_router 补挂。")
    dst = src.parent / f"layer_overlay.phase2e-stripped.json"
    dst.write_text(json.dumps(ov, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"剥离副本: {dst}（{n} regions 剥离意图，几何全保留）")
    return dst


def main() -> int:
    args = sys.argv[1:]
    tower = args[0] if args else "jc1"
    if tower not in TOWERS:
        print(f"未知 tower: {tower}（可选 {'/'.join(TOWERS)}）", file=sys.stderr)
        return 2
    cfg = TOWERS[tower]
    stripped = make_stripped(cfg["src_overlay"])

    passthru = []
    if "--" in args:
        i = args.index("--")
        passthru = args[i + 1:]
        args = args[:i]

    script = REPO / cfg["script"]
    sys.argv = [str(script)] + cfg["out_dir"] + ["--skip-sync"] + passthru
    modname = script.stem
    pkg_dir = script.parent
    sys.path.insert(0, str(pkg_dir))

    import importlib

    mod = importlib.import_module(f"scripts.{modname}")
    # 关键：overlay 指到剥离副本（同目录相对引用不断链）；脚本 main() 里
    # full_overlay() 读这个常量再 parse_all_project_sheets=True。
    mod.OVERLAY_PATH = stripped
    # OUT 常量在 main 里作为 out_dir 默认值——已用 --out-dir 覆盖，
    # 但 gt_align/production 分支会写 OUT 下的临时文件，保持独立。
    mod.OUT = REPO / "out/phase2e" / f"{tower}-tmp"
    return mod.main()


if __name__ == "__main__":
    raise SystemExit(main())
