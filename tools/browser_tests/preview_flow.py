"""Preview build: separate save file, badge, 'copy my live save' on first run, live file never touched.
Needs mock_ms.py on :8803 and a build made with --preview served on :8805 (see README)."""
import json, urllib.request, hashlib
from playwright.sync_api import sync_playwright
URL = "http://localhost:8805/"
M = "http://localhost:8803"
def mock(path, data=None, method="GET"):
    req = urllib.request.Request(M + path, data=data, method=method, headers={"Authorization": "Bearer x"})
    return urllib.request.urlopen(req).read()
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: raise SystemExit(1)

seed = open("/tmp/siteP/footsim-seed.db", "rb").read()
mock("/_set", seed, "PUT")                          # pretend this is the user's live footsim.db
live_hash = hashlib.sha256(mock("/_data")).hexdigest()

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 390, "height": 844}).new_page()
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    names = iter(["Preview edit"]); pg.on("dialog", lambda d: d.accept(next(names)) if d.type == "prompt" else d.accept())
    pg.goto(URL); pg.wait_for_selector("text=Sign in with Microsoft"); pg.click("text=Sign in with Microsoft")
    pg.wait_for_selector("text=Preview: no test save yet")
    check("first-run offers copying the live save", pg.is_visible("text=Copy my live save"))
    pg.click("text=Copy my live save"); pg.wait_for_selector("#main .card", timeout=180000)
    pg.wait_for_function("document.querySelector('.fsStatus').textContent.includes('Saved')", timeout=30000)
    other = json.loads(mock("/_other"))
    check("preview file created, separate from live", "footsim-preview.db" in other, str(other))
    check("badge shown", pg.inner_text(".fsTag").lower() == "preview")
    check("tab title marked", pg.title().startswith("Preview"), pg.title())
    pg.screenshot(path="/tmp/shot_preview.png")
    pg.click("#newBranch"); pg.wait_for_selector("#banner:has-text('Preview edit')")
    pg.click(".fsSave"); pg.wait_for_function("document.querySelector('.fsStatus').textContent.includes('Saved')", timeout=30000)
    check("live file untouched after preview save", hashlib.sha256(mock("/_data")).hexdigest() == live_hash)
    pg.reload(); pg.wait_for_selector("#main .card", timeout=180000)
    check("reload loads the preview file (has the edit)", "Preview edit" in pg.evaluate("document.querySelector('#branch').innerHTML"))
    check("no page errors", not errs, str(errs))
    b.close()
print("preview flow passed")
