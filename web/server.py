"""P0-3 产品 Demo 页后端（零依赖，stdlib）。

启动：python web/server.py [--port 8000]
流程：浏览器上传 DXF/PNG/PDF → 本服务调用 TowerHarness 跑全链 →
返回 model.json / tower.glb / steps.json / harness_summary.json 路径，
前端用 three.js 展示 GLB，并列出构件追溯（点杆件看 source）。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import sys
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))


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
        if self.path != "/api/run":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
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
                self._send(500, {"ok": False, "error": str(exc)})


def main():
    port = 8000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Demo 页已启动：http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
