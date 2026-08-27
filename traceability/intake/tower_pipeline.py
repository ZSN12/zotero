"""铁塔主路径共用管线（Phase 2/3 的 CLI 组装层）。

intake-tower 与 compile-drawing --tower 共用：
    finalize_tower_model   BOM 交叉核验 + 跨视图合并 + 注入五条规则
    evaluate_tower_model   Harness 验证 +（可选）金标准对齐
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from ..harness.harness import run_harness
from ..harness.tower_validators import inject_tower_rules
from ..model import EngineeringModel
from .tower_bom import cross_check_bom, parse_bom_auto, parse_bom_csv
from .tower_views import merge_view_bars, merge_view_coordinates


def finalize_tower_model(
    model: EngineeringModel,
    bom_path: Optional[str | Path] = None,
    merge: bool = False,
    allow_scan: bool = False,
    layer_map_path: Optional[str | Path | dict] = None,
) -> EngineeringModel:
    """BOM 交叉核验 →（可选）跨视图合并 → 注入验证规则。

    P2-5 扫描闸门：allow_scan=False 时扫描候选保持 pending_review，
    注入的 r_scan_reviewed 规则会 failed，阻断终版 export strict；
    人工确认（confirm_tower_scan）把 solve_status=verified 后，
    再以 allow_scan=True 调用即可通过闸门。

    layer_map_path：per-project overlay（P0-1），下传给 merge_view_coordinates /
    merge_view_bars，确保国网等外部图的 view_regions（含 y_expand/x_expand）
    能被读回来，而不是只查 schema/tower_layer_map.json。
    """
    if bom_path:
        model = cross_check_bom(model, parse_bom_auto(bom_path))
    if merge:
        merge_view_coordinates(model, overlay=layer_map_path)
        model = merge_view_bars(model, overlay=layer_map_path)
    inject_tower_rules(model)
    if allow_scan:
        # 只做「允许进入求解链」的标记；是否 verified 仍由 r_scan_reviewed 规则判定
        model = _allow_scan(model)
    return model


def _allow_scan(model: EngineeringModel) -> EngineeringModel:
    """把扫描候选标记为允许进终版（不改 solve_status，规则负责把关）。"""
    return model


def evaluate_tower_model(
    model: EngineeringModel,
    golden_path: Optional[str | Path] = None,
) -> Dict:
    """Harness 验证 +（可选）金标准坐标对齐。

    返回 {"results": [...], "golden": {...}|None}，不改变退出码。
    """
    from ..solve.tower_solver import compare_to_golden, solve_tower

    results = run_harness(model)
    golden = None
    if golden_path:
        nodes, _problems = solve_tower(model)
        golden = compare_to_golden(nodes, golden_path)
    return {"results": results, "golden": golden}
