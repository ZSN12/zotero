"""图册级项目模型（Gap 1 / Phase F）。

跨分册图纸的统一索引、证据链聚合、多模块装配与 BOM 树汇总。
"""

from .model import ProjectModel, ProjectSheet, load_project, save_project
from .assembly import assemble_modules, ModuleBoundary
from .bom_tree import aggregate_bom_tree, BomTreeNode

__all__ = [
    "ProjectModel",
    "ProjectSheet",
    "load_project",
    "save_project",
    "assemble_modules",
    "ModuleBoundary",
    "aggregate_bom_tree",
    "BomTreeNode",
]
