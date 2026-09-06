"""图册级 ProjectModel（Gap 1）。

一座高压塔通常由 5~20+ 张分册图纸组成。ProjectModel 提供：
    * 跨文件统一索引（sheet_id → EngineeringModel / 源路径）
    * 证据链聚合（SourceRef 按 sheet 归档）
    * 模块分段元数据（module_id、拼接面、依赖关系）
"""

from __future__ import annotations

import json
from collections import defaultdict
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
    kind: str = "drawing"          # 文件级 kind（兼容旧字段：assembly/drawing/...）
    role: str = "node_detail"      # Phase A1：规范 sheet_role 枚举
    spatial_mergeable: bool = False  # Phase A2：是否允许进入 M3 spatial_merge
    module_id: Optional[str] = None
    view_kinds: List[str] = field(default_factory=list)
    model_path: Optional[str] = None
    projection_refs: Dict[str, Any] = field(default_factory=dict)


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

    def register_module(self, module_id: str, sheet_id: str, **meta: Any) -> None:
        """登记模块段；同 module_id 下累积多张 sheet，不覆盖。"""
        mod = self.modules.setdefault(module_id, {"module_id": module_id, "sheets": []})
        sheets: List[str] = mod.setdefault("sheets", [])
        if sheet_id not in sheets:
            sheets.append(sheet_id)
        for k, v in meta.items():
            if k == "sheets":
                continue
            mod[k] = v

    def aggregate_evidence(self, model: EngineeringModel, sheet_id: str) -> Dict[str, Any]:
        """从真实投影引用（projection_refs）汇总某 sheet 的证据链。

        不再只数 SourceRef 数量——改为从每条杆件的 projection_refs 里统计
        真实跨视图投影来源（front/plan/side/detail），缺失时回退到 SourceRef。
        返回 {"refs": n, "views": {view_type: count}, "unresolved": n}。
        """
        views: Dict[str, int] = defaultdict(int)
        refs = 0
        unresolved = 0
        for comp in model.components.values():
            if comp.kind != "tower_bar":
                continue
            prs = comp.properties.get("projection_refs") or []
            if prs:
                refs += len(prs)
                for pr in prs:
                    vt = pr.get("view_type")
                    if vt:
                        views[vt] = views.get(vt, 0) + 1
            elif comp.source and comp.source.reference:
                refs += 1
                vt = comp.properties.get("view_type")
                if vt:
                    views[vt] = views.get(vt, 0) + 1
        df = model.components.get("drawing_file")
        if df is not None:
            unresolved = len(df.properties.get("unresolved_projection_refs") or [])
        summary = {"refs": refs, "views": dict(views), "unresolved": unresolved}
        if sheet_id in self.sheets:
            self.sheets[sheet_id].projection_refs = summary
        return summary

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
    # 过滤旧 JSON 里已废弃的 evidence_count 等未知字段，避免 ProjectSheet(**v) 报错。
    _sheet_fields = set(ProjectSheet.__dataclass_fields__)
    sheets = {
        k: ProjectSheet(**{f: val for f, val in v.items() if f in _sheet_fields})
        for k, v in (data.get("sheets") or {}).items()
    }
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
    from ..intake.dwg import ensure_dxf_batch
    from ..intake.tower_dxf import extract_tower_from_dxf, resolve_drawing_kind
    from ..intake.tower_spec import canonical_sheet_role, sheet_is_spatial_mergeable

    input_dir = Path(input_dir)
    out_dir = Path(out_dir or input_dir / ".project_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_dir = out_dir / "dxf"
    dxf_paths = ensure_dxf_batch(input_dir, dxf_dir)

    # Phase 2c：意图注册（overlay 未声明的 stem 由 sheet_intent 四分类
    # 补挂 view_regions）。失败不阻断交付（回退旧行为）。
    try:
        from ..intake.intent_router import register_sheet_intents
        register_sheet_intents(dxf_paths, layer_map_path)
    except Exception:
        pass

    project = ProjectModel(project_id=project_id, name=project_id)
    failures: List[Dict[str, str]] = []
    for dxf in sorted(dxf_paths):
        stem = Path(dxf).stem
        kind = resolve_drawing_kind(stem, overlay=layer_map_path)
        # P0-2：单张 sheet 解析失败不得中断整个图册交付——捕获并记录到
        # project.metadata["sheet_failures"]，由 deliver_project 汇总判 failed。
        try:
            model = extract_tower_from_dxf(dxf, layer_map_path=layer_map_path)
        except Exception as exc:
            failures.append({"stem": stem, "error": f"{type(exc).__name__}: {exc}"})
            continue
        model_path = out_dir / f"{stem}.json"
        save_model(model, model_path)
        role = kind.get("role") or canonical_sheet_role(kind["kind"])
        sheet = ProjectSheet(
            sheet_id=stem,
            path=str(dxf),
            kind=kind["kind"],
            role=role,
            spatial_mergeable=sheet_is_spatial_mergeable(stem, overlay=layer_map_path),
            module_id=_infer_module_id(stem, role=role),
            view_kinds=_view_kinds(model),
            model_path=str(model_path),
        )
        project.add_sheet(sheet)
        project.aggregate_evidence(model, stem)
        if sheet.module_id:
            project.register_module(
                sheet.module_id,
                stem,
                kind=kind["kind"],
                role=role,
            )
    if failures:
        project.metadata["sheet_failures"] = failures
    return project


def _infer_module_id(stem: str, *, role: Optional[str] = None) -> Optional[str]:
    """从文件名推断模块段（M1~M6 等，通用，不绑具体塔型）。"""
    import re
    m = re.search(r"[-_](m\d+)[-_]", stem.lower())
    if m:
        return m.group(1).upper()
    return None


def _view_kinds(model: EngineeringModel) -> List[str]:
    df = model.components.get("drawing_file")
    if df is None:
        return []
    return list(df.properties.get("view_kinds") or [])
