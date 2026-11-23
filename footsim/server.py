"""Local web editor. Standard library only.

    python -m footsim.server            # then open http://localhost:8765
"""
import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from footsim import db, dispatch
from footsim.dispatch import upgrade_branches

WEB = Path(__file__).with_name("web")
DB_PATH = db.DEFAULT_DB


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        if method == "GET" and not url.path.startswith("/api/"):
            page = WEB / ("index.html" if url.path in ("/", "") else url.path.lstrip("/"))
            if page.resolve().parent != WEB.resolve() or not page.is_file():
                return self._send(404, b"Not found", "text/plain")
            ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}.get(page.suffix[1:], "text/plain")
            return self._send(200, page.read_bytes(), ctype + "; charset=utf-8")

        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        code, data = dispatch.handle(method, self.path, body, DB_PATH)
        self._json(code, data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    global DB_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", type=Path, default=db.DEFAULT_DB)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    DB_PATH = args.db
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found. Run the importer first (see README).")
    conn = db.init_db(DB_PATH)       # adds any tables added since the database was made
    upgrade_branches(conn)
    conn.close()
    url = f"http://localhost:{args.port}"
    print(f"Football Simulator editor running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
