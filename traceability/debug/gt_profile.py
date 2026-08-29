"""GT 剖面拟合函数（阶段 0.2：GT 隔离——仅评测/调试专用）。

这些函数把 35A1-JC1 铁塔的**权威几何剖面**（塔身四棱台半宽、塔头横担外伸）
硬编码为数值公式。它们是 GT 拟合结果，**严禁在生产建模默认路径调用**：

    * 生产四面展开（expand_4_face_symmetry）默认不得传入这些函数；
    * 仅当 overlay 显式 `use_gt_half_width: true`（debug/eval）时，由
      tower_symmetry 显式注入 half_width_fn / crossarm_half_width_fn；
    * 任何经 GT 半宽展开的模型，必须在产物上打 gt_aligned=True 标记，
      正式评测脚本检测到该标记时拒绝评测。

将本模块与 debug/gt_align.py 并列：都是「评测基准 / 调试对齐」专用，不属于
生产建模能力。生产默认应从不 import 本模块。
"""

from __future__ import annotations


def gt_tower_half_width(z: float) -> float:
    """35A1-JC1 30m 铁塔的权威塔身主腿半宽（真实 mm），由 GT 剖面拟合。

    塔身（含塔头主腿）是规则四棱台，截面半宽只随标高线性收窄：
        - 塔身 z ∈ [0, 29000]：2649 - 0.0687*z（塔脚 2649 → 塔身顶 656）
        - 塔头主腿 z ∈ [30000, 36600]：662 - 0.0687*(z-30000)（延续收窄到 200）
    注意：这里不含横担（crossarm）外伸；横担是塔头段的水平悬臂，半宽会
    突然跳到 1400/1900/2200mm，需另行处理（见 gt_crossarm_half_width）。
    """
    if z < 30000.0:
        return max(0.0, 2649.0 - 0.0687 * z)
    # 塔头主腿延续（662 → 200）
    return max(0.0, 662.0 - 0.0687 * (z - 30000.0))


def gt_crossarm_half_width(z: float) -> float:
    """35A1-JC1 塔头横担（水平悬臂）的权威外伸半宽（真实 mm）。

    横担只存在于塔头段，按标高分层（外层主导值）：
        z ≈ 30000：外层 2200（下层横担）
        z ≈ 33500：外层 1900（中层横担）
        z ≈ 33850：1134（上层横担）
    其余标高无横担，返回 0。用「就近横担层」把塔头段横担节点锚定到权威外伸。
    """
    if z < 30000.0:
        return 0.0
    # 就近横担层
    layers = [(30000.0, 2200.0), (30400.0, 1403.0), (33500.0, 1900.0), (33850.0, 1134.0)]
    best_z, best_hw = min(layers, key=lambda lz: abs(lz[0] - z))
    # 若离最近横担层超过 1500mm，则无横担（塔头主腿区段）
    if abs(z - best_z) > 1500.0:
        return 0.0
    return best_hw
