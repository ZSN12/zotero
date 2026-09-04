"""图册级项目模型（Gap 1 / Phase F）。

跨分册图纸的统一索引、证据链聚合、多模块装配与 BOM 树汇总。
"""

from .model import ProjectModel, ProjectSheet, load_project, save_project
from .assembly import assemble_modules, ModuleBoundary
from .delivery import deliver_project
from .bom_tree import aggregate_bom_tree, BomTreeNode
from .bar_inventory import aggregate_bar_inventory
from .harness import run_project_harness
from .module_build import physical_bar_counts, resolve_master_bom_path, try_assembly_from_merged

__all__ = [
    "ProjectModel",
    "ProjectSheet",
    "load_project",
    "save_project",
    "assemble_modules",
    "deliver_project",
    "ModuleBoundary",
    "aggregate_bom_tree",
    "BomTreeNode",
    "aggregate_bar_inventory",
    "run_project_harness",
    "physical_bar_counts",
    "resolve_master_bom_path",
    "try_assembly_from_merged",
]
