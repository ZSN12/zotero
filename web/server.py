"""P0-3 产品 Demo 页后端（零依赖，stdlib）。

启动：python web/server.py [--port 8000]
流程：浏览器上传 DXF/PNG/PDF → 本服务调用 TowerHarness 跑全链 →
返回 model.json / tower.glb / steps.json / harness_summary.json 路径，
前端用 three.js 展示 GLB，并列出构件追溯（点杆件看 source）。

Phase E 扩展：
    * 审计日志 audit.jsonl（每次 run / confirm-scan 记录）
    * POST /api/confirm-scan 人工扫描确认（confirm_tower_scan）
"""

from __future__ import annotations

import base64
import json
import mimetypes
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))

AUDIT_PATH = Path(tempfile.gettempdir()) / "tower-demo-audit.jsonl"


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


def confirm_scan_model(model_path: Path) -> dict:
    from traceability.io import load_model, save_model
    from traceability.intake.tower_layout import confirm_tower_scan

    model = load_model(str(model_path))
    before = sum(
        1 for c in model.components.values()
        if c.properties.get("solve_status") == "pending_review"
    )
    model = confirm_tower_scan(model)
    save_model(model, str(model_path))
    after = sum(
        1 for c in model.components.values()
        if c.properties.get("solve_status") == "verified"
    )
    _audit("confirm_scan", model=str(model_path), verified=after, was_pending=before)
    return {
        "ok": True,
        "verified_components": after,
        "was_pending_review": before,
        "model_path": _public_path(str(model_path)),
    }


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
        if path.startswith("/artifacts/"):
            rel = path[len("/artifacts/"):]
            f = Path(tempfile.gettempdir()) / rel
            if f.exists() and f.is_file():
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                return self._send(200, f.read_bytes(), ctype)
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
            if not rel.startswith("/artifacts/"):
                return self._send(400, {"ok": False, "error": "model_path 无效"})
            f = Path(tempfile.gettempdir()) / rel[len("/artifacts/"):]
            if not f.exists():
                return self._send(404, {"ok": False, "error": "model 不存在"})
            try:
                payload = confirm_scan_model(f)
                self._send(200, payload)
            except Exception as exc:
                _audit("confirm_error", error=str(exc))
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
