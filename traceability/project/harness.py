"""图册级 Project Harness（M7 / Gap 1）。

在单模型 Harness 之外，对 ProjectModel + 多 sheet 汇总报告做规则验证：
    * r_project_sheets_ready       分册模型已索引
    * r_project_cross_sheet_bar_id 跨 sheet 件号重复（pending 待人工）
    * r_project_bom_master         master BOM 数量冲突
    * r_project_bar_inventory      件号索引已建立
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
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
        if r.status == ValidationStatus.FAILED:
            failed.append(r.rule_id)
    return {
        "counts": counts,
        "failed": failed,
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

    summary = _summarize(results)
    summary["modules"] = project.modules
    summary["sheet_count"] = len(project.sheets)
    if sheet_models is not None:
        summary["sheet_models_loaded"] = len(sheet_models)
    return summary
