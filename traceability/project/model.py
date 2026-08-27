"""图册级 ProjectModel（Gap 1）。

一座高压塔通常由 5~20+ 张分册图纸组成。ProjectModel 提供：
    * 跨文件统一索引（sheet_id → EngineeringModel / 源路径）
    * 证据链聚合（SourceRef 按 sheet 归档）
    * 模块分段元数据（module_id、拼接面、依赖关系）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..model import EngineeringModel, SourceRef, SourceType
from ..io import load_model, save_model


@dataclass
class ProjectSheet:
    """一张分册图纸在项目中的登记。"""
    sheet_id: str
    path: str
    kind: str = "drawing"          # assembly / module / detail / bom / title_block
    module_id: Optional[str] = None
    view_kinds: List[str] = field(default_factory=list)
    model_path: Optional[str] = None
    evidence_count: int = 0


@dataclass
class ProjectModel:
    """跨图册项目 IR。"""
    project_id: str
    name: str
    sheets: Dict[str, ProjectSheet] = field(default_factory=dict)
    modules: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    assembly_joints: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_sheet(self, sheet: ProjectSheet) -> None:
        self.sheets[sheet.sheet_id] = sheet

    def register_module(self, module_id: str, **meta: Any) -> None:
        self.modules[module_id] = {"module_id": module_id, **meta}

    def aggregate_evidence(self, model: EngineeringModel, sheet_id: str) -> int:
        """统计并标记某 sheet 模型上的 SourceRef 数量。"""
        n = 0
        for comp in model.components.values():
            if comp.source and comp.source.reference:
                n += 1
        if sheet_id in self.sheets:
            self.sheets[sheet_id].evidence_count = n
        return n

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "sheets": {k: asdict(v) for k, v in self.sheets.items()},
            "modules": self.modules,
            "assembly_joints": self.assembly_joints,
            "metadata": self.metadata,
        }


def load_project(path: str | Path) -> ProjectModel:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    sheets = {k: ProjectSheet(**v) for k, v in (data.get("sheets") or {}).items()}
    return ProjectModel(
        project_id=data["project_id"],
        name=data.get("name", data["project_id"]),
        sheets=sheets,
        modules=data.get("modules") or {},
        assembly_joints=data.get("assembly_joints") or [],
        metadata=data.get("metadata") or {},
    )


def save_project(project: ProjectModel, path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(project.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def build_project_from_directory(
    input_dir: str | Path,
    project_id: str,
    *,
    layer_map_path: Optional[str | Path] = None,
    out_dir: Optional[str | Path] = None,
) -> ProjectModel:
    """从目录批量 intake，构建 ProjectModel 索引（不自动 3D 求解）。"""
    from ..intake.tower_dxf import classify_drawing_kind, extract_tower_from_dxf
    from ..intake.dwg import ensure_dxf_batch

    input_dir = Path(input_dir)
    out_dir = Path(out_dir or input_dir / ".project_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_dir = out_dir / "dxf"
    dxf_paths = ensure_dxf_batch(input_dir, dxf_dir)

    project = ProjectModel(project_id=project_id, name=project_id)
    for dxf in sorted(dxf_paths):
        stem = Path(dxf).stem
        kind = classify_drawing_kind(stem)
        model = extract_tower_from_dxf(dxf, layer_map_path=layer_map_path)
        model_path = out_dir / f"{stem}.json"
        save_model(model, model_path)
        sheet = ProjectSheet(
            sheet_id=stem,
            path=str(dxf),
            kind=kind["kind"],
            module_id=_infer_module_id(stem),
            view_kinds=_view_kinds(model),
            model_path=str(model_path),
        )
        project.add_sheet(sheet)
        project.aggregate_evidence(model, stem)
        if sheet.module_id:
            project.register_module(
                sheet.module_id,
                sheets=[stem],
                kind=kind["kind"],
            )
    return project


def _infer_module_id(stem: str) -> Optional[str]:
    """从文件名推断模块段（M1~M6 等）。"""
    import re
    m = re.search(r"[-_](m\d+)[-_]", stem.lower())
    if m:
        return m.group(1).upper()
    if "jc1-02" in stem.lower():
        return "M1"
    if "sjg1" in stem.lower():
        return "M1"
    return None


def _view_kinds(model: EngineeringModel) -> List[str]:
    df = model.components.get("drawing_file")
    if df is None:
        return []
    return list(df.properties.get("view_kinds") or [])
