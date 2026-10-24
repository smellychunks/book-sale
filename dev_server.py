#!/usr/bin/env python3
"""Local preview server: serves app/ at / and a data folder at /data/, like the real container will.
    python dev_server.py [--data PATH] [--port 8480]       then open http://localhost:8480
localhost counts as a secure context, so the service worker and offline storage work for real.
The tests import Server and use its `faults` to simulate a slow, broken or truncated connection."""
import argparse, http.server, os, threading, time
from pathlib import Path

HERE = Path(__file__).parent


class Server:
    def __init__(self, data_dir, port=0, app_dir=HERE / "app"):
        self.data_dir, self.app_dir = Path(data_dir), Path(app_dir)
        self.csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
                    "manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.faults = {}                 # e.g. {"version": "stall"}, {"snapshot": "truncate" | "stall" | "500"}
        self.hits = []                   # every path requested, for assertions
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                outer.hits.append(path)
                if path.startswith("/data/"):
                    name = path[len("/data/"):]
                    kind = "version" if name == "version.json" else "snapshot"
                    fault = outer.faults.get(kind)
                    f = outer.data_dir / name
                    if fault == "500":
                        return self._send(500, b"boom")
                    if not f.is_file() or "/" in name:
                        return self._send(404, f"Not found: {path}\nThe data folder is {outer.data_dir}\n"
                                          "Run export_store_snapshot.py first, or start this server with --data <folder>."
                                          .encode())
                    body = f.read_bytes()
                    if fault == "stall":
                        time.sleep(outer.faults.get("stall_seconds", 4))
                    if fault == "truncate":
                        body = body[: len(body) // 2]
                    return self._send(200, body, "application/json", cache="no-store")
                f = outer.app_dir / (path.lstrip("/") or "index.html")
                if not f.is_file():
                    return self._send(404, f"Not found: {path}\nThe app folder is {outer.app_dir}\n"
                                      "It must contain index.html, app.js, styles.css, sw.js ... "
                                      "(the 'app' folder from the download sits next to dev_server.py).".encode())
                types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                         ".png": "image/png", ".webmanifest": "application/manifest+json"}
                return self._send(200, f.read_bytes(), types.get(f.suffix, "application/octet-stream"), cache="no-cache")

            def _send(self, code, body, ctype="text/plain", cache=None):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    if cache:
                        self.send_header("Cache-Control", cache)
                    self.send_header("Content-Security-Policy", outer.csp)     # same policy as nginx: catches violations
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://localhost:{self.port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    try:                                   # same default folder the exporter writes to
        from export_store_snapshot import OUTPUT_DIR as DEFAULT_DATA
    except ImportError:
        DEFAULT_DATA = HERE / "data"
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="folder export_store_snapshot.py wrote (default: its output folder)")
    ap.add_argument("--port", type=int, default=8480)
    a = ap.parse_args()
    if not (HERE / "app" / "index.html").is_file():
        raise SystemExit(f"Cannot find {HERE / 'app' / 'index.html'}\nKeep the download's layout: dev_server.py next to a folder "
                         "named 'app' that holds index.html, app.js, styles.css, sw.js, manifest.webmanifest and the icons.")
    if not (Path(a.data) / "version.json").is_file():
        print(f"WARNING: no version.json in {a.data}. The page will load but have no library. "
              "Run export_store_snapshot.py first (or pass --data <folder>).")
    s = Server(a.data, a.port).start()
    print(f"Serving app + {a.data} at {s.url}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()
