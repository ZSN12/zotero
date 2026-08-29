"""图册级 Project Harness（M7 / Gap 1）。

在单模型 Harness 之外，对 ProjectModel + 多 sheet 汇总报告做规则验证：
    * r_project_sheets_ready       分册模型已索引
    * r_project_cross_sheet_bar_id 跨 sheet 件号重复（pending 待人工）
    * r_project_bom_master         master BOM 数量冲突
    * r_project_bar_inventory      件号索引已建立
    * r_project_module_assembly    多模块 Z 向装配（M8）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..model import ValidationStatus
from .model import ProjectModel


@dataclass
class ProjectValidationResult:
    rule_id: str
    status: ValidationStatus
    message: str
    validator: str = "project-harness"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule_id,
            "status": self.status.value,
            "message": self.message,
            "validator": self.validator,
        }


def _summarize(results: List[ProjectValidationResult]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    failed: List[str] = []
    pending: List[str] = []
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
        if r.status == ValidationStatus.FAILED:
            failed.append(r.rule_id)
        elif r.status == ValidationStatus.PENDING:
            pending.append(r.rule_id)
    return {
        "counts": counts,
        "failed": failed,
        "pending": pending,
        "results": [r.to_dict() for r in results],
        "all_passed": len(failed) == 0,
    }


def run_project_harness(
    project: ProjectModel,
    *,
    sheet_models: Optional[Dict[str, Any]] = None,
    cross_sheet_bar_id: Optional[Dict[str, Any]] = None,
    bom_tree: Optional[Dict[str, Any]] = None,
    bar_inventory: Optional[Dict[str, Any]] = None,
    assembly_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """运行图册级 Harness，返回摘要 dict。"""
    results: List[ProjectValidationResult] = []

    # r_project_sheets_ready
    missing = [
        sid for sid, sheet in project.sheets.items()
        if not sheet.model_path
    ]
    if not project.sheets:
        results.append(ProjectValidationResult(
            "r_project_sheets_ready", ValidationStatus.FAILED,
            "ProjectModel 无分册",
        ))
    elif missing:
        results.append(ProjectValidationResult(
            "r_project_sheets_ready", ValidationStatus.FAILED,
            f"{len(missing)} 张分册缺少 model_path",
        ))
    else:
        results.append(ProjectValidationResult(
            "r_project_sheets_ready", ValidationStatus.PASSED,
            f"{len(project.sheets)} 张分册已索引",
        ))

    # r_project_bar_inventory
    inv_total = int((bar_inventory or {}).get("total_unique_bar_ids") or 0)
    if inv_total > 0:
        cross_n = int((bar_inventory or {}).get("cross_sheet_count") or 0)
        msg = f"件号索引 {inv_total} 个"
        if cross_n:
            msg += f"，{cross_n} 个跨 sheet"
        results.append(ProjectValidationResult(
            "r_project_bar_inventory", ValidationStatus.PASSED, msg,
        ))
    else:
        bom_total = int((bom_tree or {}).get("total_unique_bar_ids") or 0)
        if bom_total > 0:
            results.append(ProjectValidationResult(
                "r_project_bar_inventory", ValidationStatus.PASSED,
                f"BOM 树 {bom_total} 个件号",
            ))
        else:
            results.append(ProjectValidationResult(
                "r_project_bar_inventory", ValidationStatus.PENDING,
                "无 bom_row / tower_bar 件号可汇总",
            ))

    # r_project_cross_sheet_bar_id（cross_file_bar_id_report 或 bar_inventory）
    dup = int((cross_sheet_bar_id or {}).get("duplicate_count") or 0)
    inv_cross = int((bar_inventory or {}).get("cross_sheet_count") or 0)
    cross_n = max(dup, inv_cross)
    if cross_n == 0:
        results.append(ProjectValidationResult(
            "r_project_cross_sheet_bar_id", ValidationStatus.PASSED,
            "无跨 sheet 重复件号组",
        ))
    else:
        results.append(ProjectValidationResult(
            "r_project_cross_sheet_bar_id", ValidationStatus.PENDING,
            f"{cross_n} 组件号跨 sheet 出现，待人工核对",
        ))

    # r_project_bom_master
    conflicts = (bom_tree or {}).get("conflicts") or []
    master_path = (project.metadata or {}).get("master_bom_path")
    if conflicts:
        results.append(ProjectValidationResult(
            "r_project_bom_master", ValidationStatus.FAILED,
            f"{len(conflicts)} 个件号与 master BOM 数量不一致",
        ))
    elif master_path and int((bom_tree or {}).get("total_unique_bar_ids") or 0) > 0:
        only_master = (bom_tree or {}).get("only_in_master") or []
        only_model = (bom_tree or {}).get("only_in_model") or []
        if only_master or only_model:
            results.append(ProjectValidationResult(
                "r_project_bom_master", ValidationStatus.PENDING,
                f"master 核对：缺模型 {len(only_model)} / 缺 master {len(only_master)}",
            ))
        else:
            results.append(ProjectValidationResult(
                "r_project_bom_master", ValidationStatus.PASSED,
                "master BOM 数量核对通过",
            ))
    elif master_path:
        results.append(ProjectValidationResult(
            "r_project_bom_master", ValidationStatus.PENDING,
            "已指定 master BOM 但分册无 bom_row",
        ))
    else:
        results.append(ProjectValidationResult(
            "r_project_bom_master", ValidationStatus.PASSED,
            "未指定 master BOM，跳过数量核对",
        ))

    asm = assembly_info or {}
    if asm.get("enabled") or asm.get("model"):
        reports = asm.get("reports") or []
        matched = sum(int(r.get("matched") or 0) for r in reports)
        if matched > 0:
            results.append(ProjectValidationResult(
                "r_project_module_assembly", ValidationStatus.PASSED,
                f"模块装配 {matched} 对边界节点对齐（{asm.get('mode', 'assembly')}）",
            ))
        else:
            results.append(ProjectValidationResult(
                "r_project_module_assembly", ValidationStatus.PENDING,
                "模块装配已启用但未匹配边界节点",
            ))
    elif (project.metadata or {}).get("module_assembly_requested"):
        results.append(ProjectValidationResult(
            "r_project_module_assembly", ValidationStatus.PENDING,
            "enable_module_assembly 已开但缺少可装配模块",
        ))

    # Phase 3 验收：r_project_assembly_closed —— M1~M6 拼装接口对齐公差 Δ ≤ 5mm
    asm = assembly_info or {}
    if asm.get("model"):
        reports = asm.get("reports") or []
        closed = all(bool(r.get("closed")) for r in reports)
        max_gap = max((float(r.get("max_gap_mm") or 0.0) for r in reports), default=0.0)
        n_pairs = sum(int(r.get("matched") or 0) for r in reports)
        if closed and n_pairs > 0:
            results.append(ProjectValidationResult(
                "r_project_assembly_closed", ValidationStatus.PASSED,
                f"{len(reports)} 段拼装接口闭合，最大缝隙 Δ={max_gap:.2f}mm ≤ 5.0mm",
            ))
        elif n_pairs > 0:
            results.append(ProjectValidationResult(
                "r_project_assembly_closed", ValidationStatus.FAILED,
                f"拼装接口最大缝隙 Δ={max_gap:.2f}mm > 5.0mm",
            ))
        else:
            results.append(ProjectValidationResult(
                "r_project_assembly_closed", ValidationStatus.PENDING,
                "装配已启用但未匹配接口节点",
            ))

    summary = _summarize(results)
    summary["modules"] = project.modules
    summary["sheet_count"] = len(project.sheets)
    if sheet_models is not None:
        summary["sheet_models_loaded"] = len(sheet_models)
    return summary
