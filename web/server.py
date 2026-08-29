"""P0-3 产品 Demo 页后端（零依赖，stdlib）。

启动：python web/server.py [--port 8000]
流程：浏览器上传 DXF/PNG/PDF → 本服务调用 TowerHarness 跑全链 →
返回 model.json / tower.glb / steps.json / harness_summary.json 路径，
前端用 three.js 展示 GLB，并列出构件追溯（点杆件看 source）。

Phase E 扩展：
    * 审计日志 audit.jsonl（每次 run / confirm-scan 记录）
    * POST /api/confirm-scan 人工扫描确认（confirm_tower_scan）
    * POST /api/confirm-derived-y 人工复核 z-peer 插值 y（confirm_cross_file_derived_y）
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))

AUDIT_PATH = Path(tempfile.gettempdir()) / "tower-demo-audit.jsonl"
ARTIFACT_ROOT = Path(tempfile.gettempdir()).resolve()


def _resolve_artifact(rel: str) -> Optional[Path]:
    """解析 /artifacts/ 相对路径，拒绝目录穿越。"""
    if not rel.startswith("/artifacts/"):
        return None
    sub = rel[len("/artifacts/"):].replace("\\", "/").lstrip("/")
    if not sub or ".." in sub.split("/"):
        return None
    target = (ARTIFACT_ROOT / sub).resolve()
    if not str(target).startswith(str(ARTIFACT_ROOT)):
        return None
    return target


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _audit(event: str, **fields) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_pipeline(uploaded_path: Path, options: dict) -> dict:
    from traceability.harness.tower_harness import run_tower

    out_dir = Path(tempfile.gettempdir()) / f"tower-demo-{uuid.uuid4().hex[:8]}"
    result = run_tower(
        source=uploaded_path,
        out_dir=out_dir,
        bom_path=options.get("bom") or None,
        merge=bool(options.get("merge")),
        golden_path=options.get("golden") or None,
        layer_map_path=options.get("layer_map") or None,
        backend=options.get("backend") or None,
        retry=bool(options.get("retry")),
        human_review=bool(options.get("human_review")),
        format=options.get("format") or "glb",
    )
    payload = {"ok": result.get("ok"), "error": result.get("error")}
    for key in ("model_path", "steps_path", "summary_path", "glb_path"):
        p = result.get(key)
        payload[key] = _public_path(p)
    steps = result.get("graph")
    payload["steps"] = steps.to_dict() if steps else None
    payload["run_id"] = out_dir.name
    _audit("run", run_id=out_dir.name, ok=payload["ok"], source=str(uploaded_path.name))
    return payload


def confirm_scan_model(model_path: Path, *, reviewer: str = "web_user") -> dict:
    from traceability.io import load_model, save_model
    from traceability.intake.tower_layout import confirm_tower_scan

    before_hash = _file_sha256(model_path)
    model = load_model(str(model_path))
    pending_ids = [
        cid for cid, c in model.components.items()
        if c.properties.get("solve_status") == "pending_review"
        and c.kind in ("tower_bar", "tower_node")
    ]
    model = confirm_tower_scan(model)
    save_model(model, str(model_path))
    after_hash = _file_sha256(model_path)
    verified = sum(
        1 for c in model.components.values()
        if c.properties.get("solve_status") == "verified"
        and c.kind in ("tower_bar", "tower_node")
    )
    _audit(
        "confirm_scan",
        reviewer=reviewer,
        model=str(model_path),
        verified=verified,
        pending_count=len(pending_ids),
        model_hash_before=before_hash,
        model_hash_after=after_hash,
    )
    return {
        "ok": True,
        "verified_components": verified,
        "was_pending_review": len(pending_ids),
        "model_path": _public_path(str(model_path)),
        "model_hash": after_hash,
    }


def confirm_derived_y_model(model_path: Path, *, reviewer: str = "web_user") -> dict:
    from traceability.io import load_model, save_model
    from traceability.intake.tower_pipeline import (
        confirm_cross_file_derived_y,
        derived_y_pending_nodes,
    )

    before_hash = _file_sha256(model_path)
    model = load_model(str(model_path))
    pending = derived_y_pending_nodes(model)
    if not pending:
        return {
            "ok": True,
            "confirmed_nodes": 0,
            "was_pending": 0,
            "model_path": _public_path(str(model_path)),
            "model_hash": before_hash,
            "message": "无待复核插值 y 节点",
        }
    model = confirm_cross_file_derived_y(model)
    save_model(model, str(model_path))
    after_hash = _file_sha256(model_path)
    _audit(
        "confirm_derived_y",
        reviewer=reviewer,
        model=str(model_path),
        confirmed_nodes=len(pending),
        pending_nodes=pending[:10],
        model_hash_before=before_hash,
        model_hash_after=after_hash,
    )
    return {
        "ok": True,
        "confirmed_nodes": len(pending),
        "was_pending": len(pending),
        "model_path": _public_path(str(model_path)),
        "model_hash": after_hash,
    }


def export_glb_model(
    model_path: Path,
    *,
    allow_derived_y: bool = True,
    allow_scan: bool = False,
    reviewer: str = "web_user",
) -> dict:
    from traceability.io import load_model
    from traceability.intake.tower_pipeline import derived_y_pending_nodes
    from traceability.solve.tower_solver import export_tower_glb, SolveError

    model = load_model(str(model_path))
    pending_y = derived_y_pending_nodes(model)
    if pending_y and not allow_derived_y:
        return {
            "ok": False,
            "error": f"{len(pending_y)} 个插值 y 待复核，请先 confirm-derived-y",
            "derived_y_pending": len(pending_y),
        }
    glb_path = model_path.parent / "tower.glb"
    try:
        export_tower_glb(
            model,
            glb_path,
            strict=True,
            allow_scan=allow_scan,
            allow_derived_y=allow_derived_y,
        )
    except SolveError as exc:
        return {"ok": False, "error": str(exc), "derived_y_pending": len(pending_y)}
    _audit(
        "export_glb",
        reviewer=reviewer,
        model=str(model_path),
        glb=str(glb_path),
        allow_derived_y=allow_derived_y,
        derived_y_pending=len(pending_y),
    )
    return {
        "ok": True,
        "glb_path": _public_path(str(glb_path)),
        "derived_y_pending": len(pending_y),
        "allow_derived_y": allow_derived_y,
    }


def _safe_repo_path(rel: str) -> Optional[Path]:
    """只允许访问仓库内相对路径（Demo 预置样例）。"""
    if not rel or ".." in rel.split("/"):
        return None
    target = (REPO / rel).resolve()
    if not str(target).startswith(str(REPO.resolve())):
        return None
    return target


def build_project_demo(
    input_rel: str,
    *,
    layer_map_rel: Optional[str] = None,
    reviewer: str = "web_user",
) -> dict:
    from traceability.project.model import build_project_from_directory, save_project
    from traceability.intake.tower_batch import cross_file_batch

    input_dir = _safe_repo_path(input_rel)
    if input_dir is None or not input_dir.is_dir():
        return {"ok": False, "error": "input_dir 无效或越界"}
    layer_map = _safe_repo_path(layer_map_rel) if layer_map_rel else None
    out_dir = Path(tempfile.gettempdir()) / f"tower-project-{uuid.uuid4().hex[:8]}"
    project = build_project_from_directory(
        input_dir,
        project_id=input_dir.name,
        layer_map_path=str(layer_map) if layer_map else None,
        out_dir=out_dir,
    )
    project_path = out_dir / "project.json"
    save_project(project, project_path)

    cross = None
    if layer_map and layer_map.exists():
        cross_out = out_dir / "cross_file"
        cross_out.mkdir(parents=True, exist_ok=True)
        cross = cross_file_batch(input_dir, cross_out, layer_map_path=str(layer_map))

    sheets = [
        {
            "sheet_id": s.sheet_id,
            "kind": s.kind,
            "view_kinds": s.view_kinds,
            "evidence_count": s.evidence_count,
            "model_path": _public_path(s.model_path) if s.model_path else None,
        }
        for s in project.sheets.values()
    ]
    _audit("build_project", reviewer=reviewer, project=str(project_path), sheets=len(sheets))
    payload = {
        "ok": True,
        "project_path": _public_path(str(project_path)),
        "project_id": project.project_id,
        "sheets": sheets,
        "modules": project.modules,
        "out_dir": _public_path(str(out_dir)),
    }
    if cross:
        mr = cross.get("merge_report") or {}
        payload["cross_file"] = {
            "model_path": _public_path(cross.get("model_path") or ""),
            "merge_report": mr,
            "batch_report": _public_path(cross.get("batch_report") or ""),
        }
    return payload


def deliver_project_demo(
    input_rel: str,
    *,
    layer_map_rel: Optional[str] = None,
    reviewer: str = "web_user",
) -> dict:
    from traceability.project.delivery import deliver_project

    input_dir = _safe_repo_path(input_rel)
    if input_dir is None or not input_dir.is_dir():
        return {"ok": False, "error": "input_dir 无效或越界"}
    layer_map = _safe_repo_path(layer_map_rel) if layer_map_rel else None
    out_dir = Path(tempfile.gettempdir()) / f"tower-deliver-{uuid.uuid4().hex[:8]}"
    result = deliver_project(
        input_dir,
        out_dir,
        layer_map_path=str(layer_map) if layer_map else None,
    )
    _audit("deliver_project", reviewer=reviewer, ok=result.get("ok"), out=str(out_dir))
    payload = dict(result)
    for key in (
        "project_path", "model_path", "glb_path", "manifest_path",
        "canonical_glb_path", "skeleton_glb_path", "index_path",
    ):
        if payload.get(key):
            payload[key] = _public_path(str(payload[key]))
    payload["out_dir"] = _public_path(str(out_dir))
    return payload


def _public_path(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    return "/artifacts/" + p.parent.name + "/" + p.name


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            return self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path in ("/app.js", "/styles.css"):
            f = ROOT / path.lstrip("/")
            ctype = "application/javascript" if path.endswith(".js") else "text/css"
            return self._send(200, f.read_bytes(), ctype)
        if path == "/api/audit":
            rows = []
            if AUDIT_PATH.exists():
                rows = [json.loads(line) for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
            return self._send(200, {"entries": rows[-100:]})
        if path.startswith("/docs/"):
            f = REPO / path.lstrip("/")
            if f.exists() and f.is_file():
                return self._send(200, f.read_bytes(), "text/markdown; charset=utf-8")
            return self._send(404, {"error": "not found"})
        if path.startswith("/demo/"):
            f = ROOT / path.lstrip("/")
            if f.exists() and f.is_file():
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                if f.suffix == ".html":
                    ctype = "text/html; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)
            return self._send(404, {"error": "not found"})
        if path.startswith("/artifacts/"):
            target = _resolve_artifact(path)
            if target and target.is_file():
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), ctype)
            return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/run":
            filename = data.get("filename", "upload.dxf")
            data_b64 = data.get("data_b64", "")
            options = data.get("options", {})
            ext = Path(filename).suffix.lower() or ".dxf"
            with tempfile.TemporaryDirectory() as d:
                uploaded = Path(d) / ("upload" + ext)
                uploaded.write_bytes(base64.b64decode(data_b64))
                bom_path = None
                if options.get("bom_b64"):
                    bom_path = Path(d) / (options.get("bom_name", "bom.csv"))
                    bom_path.write_bytes(base64.b64decode(options["bom_b64"]))
                    options = {**options, "bom": str(bom_path)}
                try:
                    payload = run_pipeline(uploaded, options)
                    self._send(200, payload)
                except Exception as exc:
                    _audit("run_error", error=str(exc))
                    self._send(500, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/confirm-scan":
            rel = data.get("model_path", "")
            target = _resolve_artifact(rel)
            if target is None:
                return self._send(400, {"ok": False, "error": "model_path 无效或越界"})
            if not target.exists():
                return self._send(404, {"ok": False, "error": "model 不存在"})
            try:
                payload = confirm_scan_model(target, reviewer=data.get("reviewer") or "web_user")
                self._send(200, payload)
            except Exception as exc:
                _audit("confirm_error", error=str(exc))
                self._send(500, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/confirm-derived-y":
            rel = data.get("model_path", "")
            target = _resolve_artifact(rel)
            if target is None:
                return self._send(400, {"ok": False, "error": "model_path 无效或越界"})
            if not target.exists():
                return self._send(404, {"ok": False, "error": "model 不存在"})
            try:
                payload = confirm_derived_y_model(target, reviewer=data.get("reviewer") or "web_user")
                self._send(200, payload)
            except Exception as exc:
                _audit("confirm_derived_y_error", error=str(exc))
                self._send(500, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/export-glb":
            rel = data.get("model_path", "")
            target = _resolve_artifact(rel)
            if target is None:
                return self._send(400, {"ok": False, "error": "model_path 无效或越界"})
            if not target.exists():
                return self._send(404, {"ok": False, "error": "model 不存在"})
            try:
                payload = export_glb_model(
                    target,
                    allow_derived_y=bool(data.get("allow_derived_y", True)),
                    allow_scan=bool(data.get("allow_scan", False)),
                    reviewer=data.get("reviewer") or "web_user",
                )
                code = 200 if payload.get("ok") else 422
                self._send(code, payload)
            except Exception as exc:
                _audit("export_glb_error", error=str(exc))
                self._send(500, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/build-project":
            try:
                payload = build_project_demo(
                    data.get("input_dir") or "examples/external/guowang_35A1",
                    layer_map_rel=data.get("layer_map") or "examples/external/guowang_35A1/layer_overlay.json",
                    reviewer=data.get("reviewer") or "web_user",
                )
                code = 200 if payload.get("ok") else 400
                self._send(code, payload)
            except Exception as exc:
                _audit("build_project_error", error=str(exc))
                self._send(500, {"ok": False, "error": str(exc)})
            return

        if self.path == "/api/deliver-project":
            try:
                payload = deliver_project_demo(
                    data.get("input_dir") or "examples/external/guowang_35A1",
                    layer_map_rel=data.get("layer_map") or "examples/external/guowang_35A1/layer_overlay.json",
                    reviewer=data.get("reviewer") or "web_user",
                )
                code = 200 if payload.get("ok") else 422
                self._send(code, payload)
            except Exception as exc:
                _audit("deliver_project_error", error=str(exc))
                self._send(500, {"ok": False, "error": str(exc)})
            return

        return self._send(404, {"error": "not found"})


def main():
    port = 8000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Demo 页已启动：http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
