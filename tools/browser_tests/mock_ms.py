"""Fake Microsoft sign-in + OneDrive app folder, for testing only. Port 8803, CORS open."""
import json, base64, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
S = {"data": None, "etag": None, "n": 0, "puts": 0, "tokens": 0, "refreshes": 0, "expire_refresh": False, "log": []}
def b64(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization,content-type,if-match")
        self.send_header("Access-Control-Allow-Methods", "GET,PUT,POST,OPTIONS")
    def out(self, code, body=b"", ctype="application/json", extra=()):
        if not isinstance(body, bytes): body = json.dumps(body).encode()
        self.send_response(code); self.cors(); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        for k, v in extra: self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self): self.out(204)
    def authed(self):
        return self.headers.get("Authorization", "").startswith("Bearer ")
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        S["log"].append("GET " + u.path)
        if u.path.endswith("/authorize"):
            assert q["code_challenge_method"] == ["S256"] and "Files.ReadWrite.AppFolder" in q["scope"][0]
            back = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "CODE1", "state": q["state"][0]})
            return self.out(302, b"", extra=[("Location", back)])
        if u.path == "/_state":
            return self.out(200, {k: v for k, v in S.items() if k != "data"} | {"size": len(S["data"] or b"")})
        if u.path == "/dl": return self.out(200, S["data"], "application/octet-stream")
        if u.path == "/_data": return self.out(200, S["data"] or b"", "application/octet-stream")
        if u.path.startswith("/graph/me/drive/special/approot:/FootSim/footsim.db"):
            if not self.authed(): return self.out(401, {"error": {"message": "no token"}})
            if S["data"] is None: return self.out(404, {"error": {"code": "itemNotFound", "message": "not found"}})
            return self.out(200, {"eTag": S["etag"], "lastModifiedDateTime": "2026-11-23T12:00:00Z", "@microsoft.graph.downloadUrl": "http://localhost:8803/dl"})
        self.out(404, {"error": "unknown " + u.path})
    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        S["log"].append("PUT " + self.path)
        if not self.authed(): return self.out(401, {"error": {"message": "no token"}})
        if self.path.startswith("/_set"):
            S["data"] = body; S["n"] += 1; S["etag"] = f'"other{S["n"]}"'; return self.out(200, {})
        if self.path.startswith("/graph/me/drive/special/approot:/FootSim/footsim.db:/content"):
            im = self.headers.get("If-Match")
            if im and im != S["etag"]: return self.out(412, {"error": {"code": "preconditionFailed", "message": "etag mismatch"}})
            S["data"] = body; S["n"] += 1; S["puts"] += 1; S["etag"] = f'"v{S["n"]}"'
            return self.out(200, {"eTag": S["etag"]})
        self.out(404, {})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        u = urllib.parse.urlparse(self.path)
        if u.path.endswith("/token"):
            g = form["grant_type"][0]
            if g == "authorization_code":
                assert form["code_verifier"] and form["code"] == ["CODE1"]
                S["tokens"] += 1
                return self.out(200, {"access_token": "AT%d" % S["tokens"], "refresh_token": "RT1", "expires_in": 3600,
                                      "id_token": "x." + b64({"name": "Test User"}) + ".y"})
            if g == "refresh_token":
                S["refreshes"] += 1
                if S["expire_refresh"]: return self.out(400, {"error": "invalid_grant", "error_description": "expired"})
                S["tokens"] += 1
                return self.out(200, {"access_token": "AT%d" % S["tokens"], "refresh_token": "RT1", "expires_in": 3600})
        if u.path == "/_cfg":
            S.update({k: json.loads(v[0]) for k, v in form.items()}); return self.out(200, {})
        self.out(404, {})
ThreadingHTTPServer(("127.0.0.1", 8803), H).serve_forever()
