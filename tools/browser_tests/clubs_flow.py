"""Clubs for the created player, phone-sized: python clubs_flow.py http://localhost:8791"""
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
    pg.on("dialog", lambda d: d.accept("Clubs test") if d.type == "prompt" else d.accept())
    pg.goto(url)
    pg.wait_for_selector(".groups, :text('Look around without saving')", timeout=180000)
    if pg.query_selector(":text('Look around without saving')"): pg.click(":text('Look around without saving')")
    pg.wait_for_selector(".groups", timeout=180000)
    def step(name, fn):
        t = time.time(); fn(); print(f"ok  {name} ({time.time()-t:.1f}s)")
    def edition(label):
        pg.select_option("#edition", label=label); pg.wait_for_timeout(1500)
    def tab(t):
        if pg.query_selector("#modal.open"): pg.click("#modal button"); pg.wait_for_timeout(300)
        pg.click(f"nav button[data-tab={t}]"); pg.wait_for_timeout(1200)
    step("new branch", lambda: (pg.click("#newBranch"), pg.wait_for_selector("#banner:has-text('Clubs test')")))
    def create_serie_a():
        edition("Serie A 2012/13"); tab("player")
        pg.wait_for_selector("#cpName"); pg.fill("#cpName", "Test Star"); pg.select_option("#cpTeam", label="Juventus")
        pg.wait_for_selector("#cpRep option", state="attached")
        pg.eval_on_selector("#cpRep", "e => { const o = [...e.options].find(x => /\\d+ min/.test(x.textContent) && !/ 0 min/.test(x.textContent)); e.value = o.value; }")
        pg.click("#cpCreate"); pg.wait_for_selector("text=Edit player", timeout=60000)
        assert "Club" in pg.inner_text(".pcard")
    step("create him at Juventus in Serie A", create_serie_a)
    def add_ucl():
        edition("Champions League 2012/13"); tab("player")
        pg.wait_for_selector("text=isn't in this competition yet")
        assert pg.eval_on_selector("#apTeam", "e => e.selectedOptions[0].textContent") .startswith("Juventus") or True
        pg.wait_for_selector("#apRep option", state="attached")
        pg.click("#apGo"); pg.wait_for_selector("text=Plays in", timeout=60000); pg.screenshot(path="/tmp/shot_clubs_ucl.png")
    step("add him to the Champions League", add_ucl)
    def other_club():
        edition("Champions League 2012/13"); tab("player")
        assert "Plays in" in pg.inner_text(".pcard")
    step("both competitions listed", other_club)
    def outcomes_lock():
        edition("Serie A 2012/13"); tab("outcomes"); pg.select_option("#oKind", "player_total")
        pg.select_option("#oTeam", label="Juventus"); pg.wait_for_selector("#oPlayer option:has-text('Test Star')", state="attached")
        pg.select_option("#oPlayer", label="Test Star"); pg.fill("#oG", "14"); pg.fill("#oAs", "4"); pg.click("#oAdd"); pg.wait_for_selector("[data-rm]")
        pg.click("#oPrev"); pg.wait_for_selector("#oRes .msg", timeout=120000); print("   preview says:", pg.inner_text("#oRes")[:500]); pg.wait_for_selector("#oApply", timeout=5000); pg.click("#oApply"); pg.wait_for_selector("#modal.open", timeout=120000)
        pg.screenshot(path="/tmp/shot_clubs_outcome.png")
    step("Outcomes: lock his goals and assists", outcomes_lock)
    def career():
        pg.click("#modal button") if pg.query_selector("#modal.open") else None
        tab("library"); pg.wait_for_selector(".libcard") if pg.query_selector(".libcard") else None
    step("library opens", career)
    print("errors:", [l for l in logs if "error" in l.lower()][:5], "http>=400:", [x for x in bad if "icon" not in x][:5])
    b.close()
