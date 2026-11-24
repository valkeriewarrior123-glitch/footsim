import sys, time
from playwright.sync_api import sync_playwright
url = sys.argv[1]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 390, "height": 844})
    logs, bad = [], []
    pg.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
    pg.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
    pg.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    pg.on("dialog", lambda d: d.accept("Test branch") if d.type == "prompt" else d.accept())
    pg.goto(url); pg.wait_for_selector("#main .card", timeout=180000); pg.click("#topnav [data-level=tournament]"); pg.wait_for_selector(".groups")
    def step(name, fn):
        t = time.time(); fn(); print(f"ok  {name} ({time.time()-t:.1f}s)")
    step("new branch", lambda: (pg.click("#newBranch"), pg.wait_for_selector("#banner:has-text('Test branch')")))
    # edit Japan v Spain via Free path: create player first so Focus has a team? use Free mode not available w/o player -> edit any match is allowed with no player
    def edit():
        pg.click("li[data-fx]:has-text('Japan')")
        pg.wait_for_selector("#unlock, #save")
        if pg.query_selector("#unlock"): pg.click("#unlock")
        pg.wait_for_selector("#save")
        pg.screenshot(path="/tmp/shot_editor.png")
        pg.click("#roll"); pg.wait_for_selector("text=Rolled from team strength")
        pg.click("#save"); pg.wait_for_selector("#closeDiff")
        pg.click("#closeDiff")
    step("roll + save a match", edit)
    step("knockouts tab", lambda: (pg.click("#dClose"), pg.click("#subnav [data-tab=bracket]"), pg.wait_for_selector(".bracket")))
    pg.screenshot(path="/tmp/shot_bracket.png")
    def rollall():
        if pg.query_selector("#rollAll"):
            pg.click("#rollAll"); pg.wait_for_selector("#closeDiff", timeout=120000); pg.click("#closeDiff")
    step("roll remaining", rollall)
    step("stats tab", lambda: (pg.click("#subnav [data-tab=stats]"), pg.wait_for_selector("text=Scorers")))
    step("changes tab", lambda: (pg.click("#topnav [data-level=changes]"), pg.wait_for_selector("text=Across the world")))
    def player():
        pg.click("#topnav [data-level=home]"); pg.wait_for_selector("#cpName")
        pg.fill("#cpName", "Mateo Rivas"); pg.select_option("#cpTeam", "ARG"); pg.wait_for_timeout(500)
        pg.select_option("#cpPos", "FW")
        pg.click("#cpCreate"); pg.wait_for_selector("text=Mateo Rivas", timeout=60000)
    step("create player", player)
    print("dirty =", pg.evaluate("window.footsimBackend.dirty"))
    n = pg.evaluate("window.footsimBackend.exportDb().then(b => b.length)")
    print("exported db bytes:", n)
    pg.screenshot(path="/tmp/shot_player.png")
    print("bad responses:", bad)
    print("\n".join(l for l in logs if "pageerror" in l or "[error]" in l))
    b.close()
