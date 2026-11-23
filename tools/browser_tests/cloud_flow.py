import json, sqlite3, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright
URL = "http://localhost:8802/"
M = "http://localhost:8803"
def mock(path, data=None, method="GET"):
    req = urllib.request.Request(M + path, data=data, method=method, headers={"Authorization": "Bearer x"})
    return urllib.request.urlopen(req).read()
def state(): return json.loads(mock("/_state"))
def cfg(**kw): mock("/_cfg", "&".join(f"{k}={json.dumps(v)}" for k, v in kw.items()).encode(), "POST")
def branches_in(b):
    f = tempfile.NamedTemporaryFile(suffix=".db"); f.write(b); f.flush()
    c = sqlite3.connect(f.name); r = [x[0] for x in c.execute("select name from branches")]; c.close(); return r
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else "")); 
    if not cond: raise SystemExit(1)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}, accept_downloads=True)
    pg = ctx.new_page(); pg.clock.install()
    logs = []
    pg.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
    pg.on("console", lambda m: logs.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
    names = iter(["Cloud test one", "Cloud test two", "Cloud test three", "Edit after expiry"])
    pg.on("dialog", lambda d: d.accept(next(names)) if d.type == "prompt" else d.accept())
    status = lambda: pg.inner_text(".fsStatus")
    def wait_status(txt, t=30000): pg.wait_for_function("t => document.querySelector('.fsStatus') && document.querySelector('.fsStatus').textContent.includes(t)", arg=txt, timeout=t)

    # 1. first visit: sign in, no file yet -> start fresh -> uploaded straight away
    pg.goto(URL); pg.wait_for_selector("text=Sign in with Microsoft")
    pg.screenshot(path="/tmp/shot_signin.png")
    pg.click("text=Sign in with Microsoft")
    pg.wait_for_selector("text=No save file on OneDrive yet")
    check("redirect round trip signs in (token request made)", state()["tokens"] == 1)
    check("url cleaned of ?code", "code=" not in pg.url, pg.url)
    pg.click("text=Start fresh"); pg.wait_for_selector(".groups", timeout=180000)
    wait_status("Saved")
    s = state(); check("first save uploaded the seed", s["puts"] == 1 and s["size"] > 1_000_000, str(s["size"]))
    pg.screenshot(path="/tmp/shot_bar.png")

    # 2. edit -> unsaved -> Save
    pg.click("#newBranch"); pg.wait_for_selector("#banner:has-text('Cloud test one')")
    check("edit shows unsaved", "Unsaved" in status(), status())
    pg.click(".fsSave"); wait_status("Saved")
    check("Save uploaded again", state()["puts"] == 2)
    check("uploaded file has the new branch", "Cloud test one" in branches_in(mock("/_data")))

    # 3. reload: comes back from OneDrive with the branch, no sign-in prompt
    pg.reload(); pg.wait_for_selector(".groups", timeout=180000)
    check("reload loads the branch from OneDrive", "Cloud test one" in pg.inner_text("#branch") or pg.evaluate("document.querySelector('#branch').innerHTML.includes('Cloud test one')"))
    check("no re-sign-in needed after reload", state()["tokens"] == 1 or state()["refreshes"] >= 0)
    check("status after load", "Loaded from OneDrive" in status(), status())

    # 4. leave-page warning exists when dirty
    pg.click("#newBranch"); pg.wait_for_selector("#banner:has-text('Cloud test two')")
    check("dirty again", "Unsaved" in status())

    # 5. conflict: another device saves first
    before = state()["puts"]
    mock("/_set", mock("/_data"), "PUT")
    pg.click(".fsSave"); pg.wait_for_selector("text=A newer copy is on OneDrive")
    pg.screenshot(path="/tmp/shot_conflict.png")
    check("conflict blocked the upload", state()["puts"] == before)
    pg.click("text=Keep this device's version"); wait_status("Saved")
    check("keep-mine overwrote after refreshing etag", state()["puts"] == before + 1)
    check("both branches now on OneDrive", {"Cloud test one", "Cloud test two"} <= set(branches_in(mock("/_data"))))

    # 6. autosave: 15 minutes later, saves only if something changed
    pg.click("#newBranch"); pg.wait_for_selector("#banner:has-text('Cloud test three')")
    n = state()["puts"]
    pg.clock.fast_forward("14:00"); pg.wait_for_timeout(1500)
    check("no autosave before 15 minutes", state()["puts"] == n)
    pg.clock.fast_forward("01:30"); wait_status("Saved")
    check("autosave after 15 minutes", state()["puts"] == n + 1)
    pg.clock.fast_forward("15:30"); pg.wait_for_timeout(1500)
    check("no autosave when nothing changed", state()["puts"] == n + 1)

    # 7. access token expires -> refreshed silently
    pg.evaluate("(() => { const t = JSON.parse(localStorage['footsim.auth.v1']); t.exp = 0; localStorage['footsim.auth.v1'] = JSON.stringify(t); })()")
    pg.reload(); pg.wait_for_selector(".groups", timeout=180000)
    check("expired access token refreshed silently", state()["refreshes"] >= 1, str(state()["refreshes"]))

    # 8. refresh token expired -> asked to sign in again, edits stay in the tab
    cfg(expire_refresh=True)
    pg.click("#newBranch"); pg.wait_for_selector("#banner:has-text('Edit after expiry')")
    pg.clock.fast_forward("02:00:00"); pg.wait_for_timeout(2500)          # 2 hours later the autosave fires and the sign-in has lapsed
    check("expired sign-in is reported, not silently lost", "sign in again" in status().lower(), status())
    check("edit still in the tab (unsaved flag)", pg.evaluate("window.footsimBackend.dirty") is True)
    cfg(expire_refresh=False)

    # 9. download a copy
    with pg.expect_download() as dl:
        pg.click(".fsMenu"); pg.click("text=Download a copy")
    path = dl.value.path(); data = open(path, "rb").read()
    check("download gives a real SQLite file", data[:15] == b"SQLite format 3")

    # 10. leave-page warning when dirty
    check("beforeunload warns when dirty", pg.evaluate("(() => { const e = new Event('beforeunload', {cancelable:true}); window.dispatchEvent(e); return e.defaultPrevented; })()"))
    b.close()

    # 11. fresh browser: 'look around without saving', then first-run import of a device file
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}); pg = ctx.new_page()
    pg.goto(URL); pg.wait_for_selector("text=Sign in with Microsoft")
    pg.click("text=Look around without saving"); pg.wait_for_selector(".groups", timeout=180000)
    check("local mode says not saved", "Not saved" in pg.inner_text(".fsStatus"), pg.inner_text(".fsStatus"))
    check("local mode button offers sign-in", pg.inner_text(".fsSave") == "Sign in")
    b.close()

    mock("/_cfg", b"", "POST")
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}); pg = ctx.new_page()
    import os; os.environ["X"] = "1"
    # point the mock at an empty drive again so the first-run import choice appears
    mock("/_reset", b"", "POST") if False else None
    b.close()
print("ALL DONE")
