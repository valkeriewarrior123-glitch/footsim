"""Player library, end to end in a phone-sized browser: python library_flow.py http://localhost:8791"""
import sys, time
from playwright.sync_api import sync_playwright
url = sys.argv[1]
names = iter(["Branch A", "Branch B"])
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 390, "height": 844})
    logs, bad = [], []
    pg.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
    pg.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))
    pg.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    pg.on("dialog", lambda d: d.accept(next(names)) if d.type == "prompt" else d.accept())
    pg.goto(url); pg.wait_for_selector(".groups", timeout=180000)
    def step(name, fn):
        t = time.time(); fn(); print(f"ok  {name} ({time.time()-t:.1f}s)")
    def lib(): pg.click("nav button[data-tab=library]"); pg.wait_for_selector("#libNew")
    def make_profile():
        lib(); pg.click("#libNew"); pg.wait_for_selector("#pfName")
        pg.fill("#pfName", "Mateo Rivas"); pg.fill("#pfKnown", "Rivas"); pg.select_option("#pfPos", "FW")
        pg.click("#pfRoles .chip[data-role=ST]"); pg.click("#pfSave"); pg.wait_for_selector(".libcard:has-text('Rivas')")
    step("create profile", make_profile)
    def use(branch_name, team):
        pg.click("#newBranch"); pg.wait_for_selector(f"#banner:has-text('{branch_name}')")
        lib(); pg.click("[data-use]"); pg.wait_for_selector("h3:has-text('in this branch')")
        assert pg.input_value("#cpName") == "Mateo Rivas"
        pg.select_option("#cpTeam", team); pg.wait_for_timeout(600)
        pg.click("#cpCreate"); pg.wait_for_selector("#editPlayer", timeout=60000)
    step("use in branch A (Argentina)", lambda: use("Branch A", "ARG"))
    step("use in branch B (France)", lambda: use("Branch B", "FRA"))
    pg.screenshot(path="/tmp/shot_lib_player.png")
    assert pg.query_selector("text=in sync")
    def edit():
        lib(); pg.click("[data-edit]"); pg.wait_for_selector("#pfOvr")
        pg.fill("#pfOvr", "95"); pg.click("#pfSave"); pg.wait_for_selector("#syncGo")
        pg.screenshot(path="/tmp/shot_lib_sync.png")
        boxes = pg.query_selector_all(".syncCk"); assert len(boxes) == 2, len(boxes)
        boxes[1].uncheck()                                  # only Branch A follows
        pg.click("#syncGo"); pg.wait_for_selector("#modal.open", state="detached")
    step("edit profile, update only one branch", edit)
    def check():
        pg.wait_for_timeout(1500)                           # the list redraws after the update
        n = len(pg.query_selector_all(".libcard .badge.warn")); assert n == 1, (n, pg.inner_text(".libcard"))
        pg.screenshot(path="/tmp/shot_lib_list.png")
    step("one branch still differs", check)
    def view_b():
        pg.click("nav button[data-tab=player]"); pg.wait_for_selector("#ovr")
        assert pg.input_value("#ovr") == "83", pg.input_value("#ovr")        # Branch B kept the old value
        assert pg.query_selector("text=differs from profile")
    step("branch B unchanged", view_b)
    print("bad responses:", bad)
    print("\n".join(l for l in logs if "pageerror" in l or "[error]" in l))
    b.close()
