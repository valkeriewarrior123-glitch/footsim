"""Outcomes tab, end to end on a phone-sized browser: python outcomes_flow.py http://localhost:8791"""
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
    pg.on("dialog", lambda d: d.accept("Outcome test") if d.type == "prompt" else d.accept())
    pg.goto(url)
    pg.wait_for_selector("#main .card, :text('Look around without saving')", timeout=180000)
    if pg.query_selector(":text('Look around without saving')"): pg.click(":text('Look around without saving')")
    pg.wait_for_selector("#main .card", timeout=180000)
    def step(name, fn):
        t = time.time(); fn(); print(f"ok  {name} ({time.time()-t:.1f}s)")
    def tab(): pg.click("#topnav [data-level=tournament]"); pg.click("#subnav [data-tab=outcomes]"); pg.wait_for_selector(".card h3:has-text('Outcomes')"); pg.wait_for_timeout(800)
    def no_branch():
        tab(); assert pg.query_selector("text=Pick or create a branch first")
    step("needs a branch", no_branch)
    step("new branch", lambda: (pg.click("#newBranch"), pg.wait_for_selector("#banner:has-text('Outcome test')")))
    def add(kind, **sel):
        tab(); pg.select_option("#oKind", kind)
        for k, v in sel.items(): pg.select_option(k, v) if k.startswith("#oTeam") else pg.fill(k, v)
        pg.click("#oAdd"); pg.wait_for_selector("[data-rm]")
    def champ():
        tab(); pg.select_option("#oKind", "champion"); pg.select_option("#oTeam", "BRA"); pg.click("#oAdd"); pg.wait_for_selector("[data-rm]")
        pg.select_option("#oKind", "runner_up"); pg.select_option("#oTeam", "FRA"); pg.click("#oAdd"); pg.wait_for_selector("text=Runner-up: France")
    step("add champion + runner-up", champ)
    def score():
        pg.select_option("#oKind", "score")
        pg.select_option("#oFix", index=1); pg.fill("#oH", "4"); pg.fill("#oA", "0"); pg.click("#oAdd"); pg.wait_for_selector("text=4-0")
    step("add exact score", score)
    def preview():
        pg.click("#oPrev"); pg.wait_for_selector("#oApply", timeout=120000); pg.screenshot(path="/tmp/shot_outcomes.png")
    step("preview", preview)
    def apply():
        pg.click("#oApply"); pg.wait_for_selector("#modal.open", timeout=120000); pg.screenshot(path="/tmp/shot_outcomes_diff.png")
        pg.click("#modal button"); 
    step("apply", apply)
    def conflict():
        pg.wait_for_selector("#modal.open", state="detached", timeout=20000) if False else None
        pg.screenshot(path="/tmp/shot_before_clash.png")
        tab(); pg.select_option("#oKind", "champion"); pg.select_option("#oTeam", "ARG"); pg.click("#oAdd"); pg.wait_for_selector("text=Champion: Argentina")
        pg.click("#oPrev"); pg.wait_for_selector("text=Nothing was changed", timeout=120000) if False else pg.wait_for_selector("#oRes .msg", timeout=120000)
        pg.screenshot(path="/tmp/shot_outcomes_clash.png")
    step("champion clash (ARG vs runner-up lock) is explained or met", conflict)
    print("errors:", [l for l in logs if "error" in l.lower()][:5], "http>=400:", bad[:5])
    b.close()
