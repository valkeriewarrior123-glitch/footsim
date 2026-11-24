"""Player page and clubs, phone-sized: python clubs_flow.py http://localhost:8791
Timeline -> Player -> Tournaments: create him on the Player page, add his other competition, lock his goals in Outcomes."""
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
    pg.wait_for_selector("#main .card, :text('Look around without saving')", timeout=180000)
    if pg.query_selector(":text('Look around without saving')"): pg.click(":text('Look around without saving')")
    pg.wait_for_selector("#main .card", timeout=180000)
    def side(sel):
        if not pg.is_visible("#side"): pg.click("#menuBtn"); pg.wait_for_timeout(300)
        pg.click(sel)
    def step(name, fn):
        t = time.time(); fn(); print(f"ok  {name} ({time.time()-t:.1f}s)")
    def close_modal():
        pg.wait_for_timeout(1000)
        for _ in range(3):
            b = pg.query_selector("#modal button:visible")
            if not b: break
            b.click(); pg.wait_for_timeout(400)
    def top(go): close_modal(); side(f"#side [data-go={go}]"); pg.wait_for_timeout(1200)
    def home_without_branch():
        assert "all tournaments" in pg.inner_text("#main").lower()
    step("real history lands on All tournaments", home_without_branch)
    step("new branch", lambda: (pg.click("#newBranch"), pg.wait_for_selector("#banner:has-text('Clubs test')")))
    def create_serie_a():
        pg.wait_for_selector("#cpEd", timeout=15000); pg.select_option("#cpEd", label="Serie A 2012/13"); pg.wait_for_timeout(1500)
        pg.fill("#cpName", "Test Star"); pg.select_option("#cpTeam", label="Juventus"); pg.wait_for_timeout(1500)
        pg.wait_for_selector("#cpRep option", state="attached")
        pg.eval_on_selector("#cpRep", "e => { const o = [...e.options].find(x => /\\d+ min/.test(x.textContent) && !/ 0 min/.test(x.textContent)); e.value = o.value; }")
        pg.click("#cpCreate"); pg.wait_for_selector("text=His seasons", timeout=60000)
        assert "2012/13" in pg.inner_text("#main")
    step("create him on the Player page (first competition Serie A)", create_serie_a)
    def add_ucl():
        card = pg.locator(".libcard:has-text('Champions League 2012/13')")
        card.locator("[data-add]").click(); pg.wait_for_selector("text=isn't in this competition yet")
        pg.wait_for_selector("#apRep option", state="attached")
        pg.click("#apGo"); pg.wait_for_selector("text=Plays in", timeout=60000); pg.screenshot(path="/tmp/shot_clubs_ucl.png")
    step("add him to the Champions League (opens that tournament)", add_ucl)
    def back_home():
        close_modal(); side("#side [data-go=home]"); pg.wait_for_selector("text=His seasons")
        assert "Champions League" in pg.inner_text("#side") and "Serie A" in pg.inner_text("#side"), pg.inner_text("#side")
        pg.screenshot(path="/tmp/shot_player_home.png")
    step("back on his page both competitions show", back_home)
    def outcomes_lock():
        pg.wait_for_timeout(1500)
        side("#side [data-go=season]"); pg.wait_for_selector(".libcard:has-text('Serie A') [data-open][data-tab=outcomes]"); pg.locator(".libcard:has-text('Serie A') [data-open][data-tab=outcomes]").dispatch_event("click"); pg.wait_for_selector("#oKind")
        pg.select_option("#oKind", "player_total")
        pg.select_option("#oTeam", label="Juventus"); pg.wait_for_selector("#oPlayer option:has-text('Test Star')", state="attached")
        pg.select_option("#oPlayer", label="Test Star"); pg.fill("#oG", "14"); pg.fill("#oAs", "4"); close_modal(); pg.click("#oAdd"); pg.wait_for_selector("[data-rm]")
        pg.click("#oPrev"); pg.wait_for_selector("#oApply", timeout=120000); pg.click("#oApply"); pg.wait_for_selector("#modal button:visible", timeout=120000)
        pg.screenshot(path="/tmp/shot_clubs_outcome.png")
    step("Outcomes from his page: lock his goals and assists", outcomes_lock)
    def tournaments():
        top("all"); pg.click("#main [data-open=WC2022]"); pg.wait_for_selector(".groups", timeout=60000)
        top("home"); pg.wait_for_selector("text=His seasons")
    step("Tournaments button shows every tournament, Player page is level 2", tournaments)
    def library():
        top("library"); pg.wait_for_selector("#libNew")
    step("library opens", library)
    print("errors:", [l for l in logs if "error" in l.lower() and "404" not in l][:5], "http>=400:", [x for x in bad if "icon" not in x][:5])
    b.close()
