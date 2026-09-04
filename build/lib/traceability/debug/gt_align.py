"""调试 / 评测专用工具（阶段 0.2：GT 隔离）。

生产交付路径（deliver_project / run-tower）禁止 import 本模块。
`align_skeleton_to_canonical` 只能用于：
    * 调试
    * 误差分析
    * 评测对齐（alignment for evaluation）

任何进入正常交付的结果如果被 GT 对齐过，manifest 必须标记
`gt_aligned=True`，正式评测脚本检测到该标记时必须拒绝评测。
"""

from __future__ import annotations

from typing import Any

from ..model import EngineeringModel


def align_skeleton_to_canonical(model: EngineeringModel, canonical: Any) -> None:
    """用 CanonicalTower（.mod/.NODE 权威拓扑）重建骨架的 tower_node/tower_bar。

    原地替换 model 的 tower_node / tower_bar 组件为 GT 权威拓扑。保留其它
    上下文组件（gusset/bolt/BOM 等）。GT 自带的 section / material 一并写入。

    ⚠ 本函数仅供调试/评测，严禁在生产建模路径调用：
        * 每根替换杆件都标记 gt_aligned=True
        * 评测脚本检测到 gt_aligned=True 时直接拒绝评测
    """
    for cid in list(model.components):
        if model.components[cid].kind in ("tower_node", "tower_bar"):
            del model.components[cid]

    gt_model = canonical.to_engineering_model(prefix="gt_")
    for cid, comp in gt_model.components.items():
        comp.properties["gt_aligned"] = True
        comp.properties["geometry_class"] = "canonical"
        comp.properties["geometry_origin"] = "gim"
        model.components[cid] = comp

    # 模型级标记：任何消费方（评测/manifest）都能一眼看到发生过 GT 对齐。
    df = model.components.get("drawing_file")
    if df is not None:
        df.properties["gt_aligned"] = True
