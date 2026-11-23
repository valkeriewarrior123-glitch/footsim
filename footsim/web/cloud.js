// Microsoft sign-in and OneDrive save for the static (GitHub Pages) build.
//
// Signs in with the standard OAuth "authorization code + PKCE" flow (the same flow MSAL.js uses for a
// single-page app, written out here so the site has no outside script to load), then keeps footsim.db in
// the OneDrive *app folder*:  Apps/<your app registration name>/FootSim/footsim.db
//
// Config comes from window.FOOTSIM_CONFIG.cloud (written by build_site.py). With no clientId this file does
// nothing, so the local server and plain builds are unaffected.
//
//   cloud.prepare()      before the engine starts: handles the sign-in return trip, asks to sign in if needed,
//                        and resolves with the bytes of the saved footsim.db (or null = start from the seed)
//   cloud.attach(backend) after the engine is up: adds the Save bar, 15-minute auto-save, leave-page warning
(() => {
  const cfg = window.FOOTSIM_CONFIG && window.FOOTSIM_CONFIG.cloud;
  if (!cfg || !cfg.clientId) return;
  const C = Object.assign({
    authority: "https://login.microsoftonline.com/common",
    graph: "https://graph.microsoft.com/v1.0",
    scope: "openid profile offline_access User.Read Files.ReadWrite.AppFolder",
    folder: "FootSim", file: "footsim.db", autosaveMinutes: 15,
  }, cfg);
  const redirectUri = C.redirectUri || (location.origin + location.pathname.replace(/index\.html$/, ""));
  const TOK = "footsim.auth.v1", FLOW = "footsim.flow.v1";
  const SQLITE_MAGIC = "SQLite format 3\u0000";

  // ---------- small helpers
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode: stay signed in for this tab only */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } },
  };
  const b64url = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const rand = n => b64url(crypto.getRandomValues(new Uint8Array(n)));
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const clock = d => d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const isSqlite = u8 => u8.length > 16 && String.fromCharCode(...u8.slice(0, 16)) === SQLITE_MAGIC;

  // ---------- sign-in (authorization code + PKCE)
  let tok = store.get(TOK);                      // {access, exp, refresh, name}
  class SignInNeeded extends Error {}

  async function tokenRequest(params) {
    const r = await fetch(C.authority + "/oauth2/v2.0/token", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(Object.assign({ client_id: C.clientId, scope: C.scope }, params)),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(j.error_description || j.error || ("Sign-in failed (" + r.status + ")")); e.code = j.error; throw e; }
    return j;
  }
  function keep(r) {
    let name = tok && tok.name;
    try { const p = JSON.parse(atob(r.id_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); name = p.name || p.preferred_username || name; } catch (e) { /* no id_token on refresh */ }
    tok = { access: r.access_token, exp: Date.now() + Math.max(60, (r.expires_in || 3600) - 120) * 1000, refresh: r.refresh_token || (tok && tok.refresh), name };
    store.set(TOK, tok);
  }
  function forget() { tok = null; store.del(TOK); }

  async function signIn() {
    const verifier = rand(48), state = rand(16);
    const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    store.set(FLOW, { verifier, state });
    const q = new URLSearchParams({ client_id: C.clientId, response_type: "code", redirect_uri: redirectUri, response_mode: "query",
      scope: C.scope, state, code_challenge: challenge, code_challenge_method: "S256" });
    location.assign(C.authority + "/oauth2/v2.0/authorize?" + q);
    await new Promise(() => {});                 // the page is leaving
  }

  // The page reloads with ?code=... after Microsoft's sign-in screen.
  async function handleRedirect() {
    const u = new URL(location.href);
    const code = u.searchParams.get("code"), state = u.searchParams.get("state"), err = u.searchParams.get("error");
    if (!code && !err) return;
    const flow = store.get(FLOW); store.del(FLOW);
    history.replaceState(null, "", location.pathname);       // drop the code from the address bar
    if (err) throw new Error(u.searchParams.get("error_description") || err);
    if (!flow || flow.state !== state) throw new Error("That sign-in didn't match this browser tab. Please try again.");
    keep(await tokenRequest({ grant_type: "authorization_code", code, redirect_uri: redirectUri, code_verifier: flow.verifier }));
  }

  async function accessToken() {
    if (!tok) throw new SignInNeeded("Not signed in");
    if (tok.access && Date.now() < tok.exp) return tok.access;
    if (!tok.refresh) { forget(); throw new SignInNeeded("Please sign in again"); }
    try { keep(await tokenRequest({ grant_type: "refresh_token", refresh_token: tok.refresh })); }
    catch (e) {
      if (e.code === "invalid_grant" || e.code === "interaction_required") { forget(); throw new SignInNeeded("Your Microsoft sign-in expired. Please sign in again."); }
      throw e;                                   // offline or a hiccup: keep the sign-in and try again later
    }
    return tok.access;
  }

  // ---------- OneDrive (Microsoft Graph, app folder)
  const ITEM = "/me/drive/special/approot:/" + C.folder + "/" + C.file;
  async function graph(path, init, retry = true) {
    const r = await fetch(C.graph + path, Object.assign({}, init, { headers: Object.assign({ Authorization: "Bearer " + await accessToken() }, init && init.headers) }));
    if (r.status === 401 && retry) { tok.exp = 0; return graph(path, init, false); }
    return r;
  }
  async function readError(r) { const j = await r.json().catch(() => null); return (j && j.error && j.error.message) || ("OneDrive said " + r.status); }

  async function remoteInfo() {                  // -> {etag, modified, url} or null when there's no file yet
    const r = await graph(ITEM);
    if (r.status === 404) return null;
    if (!r.ok) throw new Error(await readError(r));
    const j = await r.json();
    return { etag: j.eTag, modified: j.lastModifiedDateTime, url: j["@microsoft.graph.downloadUrl"] };
  }
  async function remoteBytes(info) {
    const r = info.url ? await fetch(info.url) : await graph(ITEM + ":/content");
    if (!r.ok) throw new Error("Couldn't download your save file (" + r.status + ")");
    return new Uint8Array(await r.arrayBuffer());
  }
  class Conflict extends Error {}
  async function remoteWrite(bytes, etag) {      // etag = the version we last saw, so a newer save from another device isn't overwritten
    const r = await graph(ITEM + ":/content", { method: "PUT", body: bytes,
      headers: Object.assign({ "Content-Type": "application/octet-stream" }, etag ? { "If-Match": etag } : {}) });
    if (r.status === 412) throw new Conflict("OneDrive has a newer copy");
    if (!r.ok) throw new Error(await readError(r));
    const j = await r.json();
    return j.eTag;
  }

  // ---------- dialogs (full-screen, so they also work over the loading screen)
  function ask(title, body, buttons) {           // buttons: [{label, value, primary}]
    return new Promise(resolve => {
      const el = document.createElement("div");
      el.style.cssText = "position:fixed;inset:0;z-index:200;display:grid;place-items:center;background:rgba(20,26,23,.55);padding:16px;font:15px system-ui,sans-serif";
      el.innerHTML = `<div style="background:var(--panel,#fff);color:var(--ink,#1d2320);border-radius:12px;padding:20px;max-width:420px;width:100%;box-shadow:0 10px 40px rgba(0,0,0,.3)">
        <div style="font-size:18px;font-weight:700;margin-bottom:8px">${esc(title)}</div>
        <div style="color:var(--muted,#5b6660);margin-bottom:16px;line-height:1.45">${body}</div>
        <div style="display:flex;flex-direction:column;gap:8px"></div></div>`;
      const row = el.lastElementChild.lastElementChild;
      buttons.forEach(b => {
        const btn = document.createElement("button");
        btn.textContent = b.label;
        btn.style.cssText = "padding:11px 14px;border-radius:8px;font:inherit;cursor:pointer;border:1px solid var(--line,#d8d4c8);" +
          (b.primary ? "background:var(--pitch,#1f6b45);color:#fff;border-color:transparent;font-weight:600" : "background:var(--panel,#fff);color:inherit");
        btn.onclick = () => { el.remove(); resolve(b.value); };
        row.appendChild(btn);
      });
      document.body.appendChild(el);
    });
  }
  function pickFile() {
    return new Promise(resolve => {
      const inp = document.createElement("input");
      inp.type = "file"; inp.accept = ".db,.sqlite,application/octet-stream";
      inp.onchange = async () => { const f = inp.files[0]; resolve(f ? new Uint8Array(await f.arrayBuffer()) : null); };
      inp.addEventListener("cancel", () => resolve(null));
      inp.click();
    });
  }

  // ---------- startup (before the engine loads)
  const S = { mode: "cloud", etag: null, firstSave: false, lastSaved: null, busy: false, conflict: false, error: "" };

  async function prepare() {
    let problem = "";
    try { await handleRedirect(); } catch (e) { problem = e.message; }
    for (;;) {
      if (!tok) {
        const c = await ask("Sign in to load your save",
          (problem ? `<div style="color:#b3261e;margin-bottom:8px">${esc(problem)}</div>` : "") +
          "Your simulation is stored in your own OneDrive, so it follows you to any device.",
          [{ label: "Sign in with Microsoft", value: "in", primary: true }, { label: "Look around without saving", value: "local" }]);
        if (c === "local") { S.mode = "local"; return null; }
        await signIn();
      }
      try {
        const info = await remoteInfo();
        if (info) { S.etag = info.etag; return await remoteBytes(info); }
        const c = await ask("No save file on OneDrive yet",
          (problem ? `<div style="color:#b3261e;margin-bottom:8px">${esc(problem)}</div>` : "") + "This looks like your first time here on this account.",
          [{ label: "Start fresh (World Cup 2022 history)", value: "fresh", primary: true }, { label: "Import my footsim.db from this device", value: "import" }]);
        problem = ""; S.firstSave = true;
        if (c === "import") {
          const bytes = await pickFile();
          if (!bytes) { problem = "No file chosen."; continue; }
          if (!isSqlite(bytes)) { problem = "That file isn't a footsim.db database."; continue; }
          return bytes;
        }
        return null;
      } catch (e) {
        if (e instanceof SignInNeeded) { problem = e.message; continue; }
        const c = await ask("Couldn't reach OneDrive", esc(e.message),
          [{ label: "Try again", value: "retry", primary: true }, { label: "Continue without saving", value: "local" }]);
        if (c === "local") { S.mode = "local"; return null; }
      }
    }
  }

  // ---------- the Save bar (after the engine is up)
  let backend, bar, $status, $save, $menu, $panel;

  function render() {
    if (!bar) return;
    const signed = !!tok && S.mode === "cloud";
    let text, cls = "";
    if (S.mode === "local") { text = "Not saved: changes stay in this tab"; cls = "warn"; }
    else if (S.error) { text = S.error; cls = "bad"; }
    else if (S.busy) text = "Saving…";
    else if (S.conflict) { text = "OneDrive has a newer copy. Tap Save to choose"; cls = "warn"; }
    else if (backend.dirty) { text = "Unsaved changes"; cls = "warn"; }
    else text = S.lastSaved ? "Saved " + clock(S.lastSaved) : "Loaded from OneDrive";
    $status.textContent = text; $status.className = "fsStatus " + cls;
    $save.textContent = signed ? "Save" : "Sign in";
    $save.disabled = S.busy;
    $save.classList.toggle("primary", signed ? backend.dirty || S.conflict : true);
    $panel.querySelector("[data-act=out]").style.display = tok ? "" : "none";
    $panel.querySelector("[data-who]").textContent = tok ? (tok.name || "Signed in") : "Not signed in";
  }

  async function save(manual) {
    if (S.busy) return;
    if (S.mode !== "cloud" || !tok) { if (manual) { await signIn(); } return; }
    if (!manual && (S.conflict || !backend.dirty)) return;
    S.busy = true; S.error = ""; render();
    let clash = false;
    try {
      const rev = backend.rev;
      const bytes = await backend.exportDb();
      S.etag = await remoteWrite(bytes, S.etag);
      S.lastSaved = new Date(); S.conflict = false; S.firstSave = false;
      if (backend.rev === rev) backend.markClean();        // edits made while saving keep the "unsaved" flag
    } catch (e) {
      if (e instanceof Conflict) { S.conflict = true; clash = manual; }
      else if (e instanceof SignInNeeded) { S.error = e.message; }
      else S.error = "Couldn't save: " + e.message;
    } finally { S.busy = false; render(); }
    if (clash) await resolveConflict();                    // after the "Saving…" state is released, so the retry can run
  }

  async function resolveConflict() {
    const c = await ask("A newer copy is on OneDrive",
      "It was saved from another device (or another tab) after you opened this one. Which should win?",
      [{ label: "Keep this device's version (replace OneDrive)", value: "mine", primary: true },
       { label: "Load the OneDrive version (lose changes made here)", value: "theirs" }, { label: "Cancel", value: "no" }]);
    if (c === "no") return;
    const info = await remoteInfo();
    if (c === "theirs" && info) {
      await backend.importDb(await remoteBytes(info)); S.etag = info.etag; S.conflict = false; S.lastSaved = new Date();
      await ask("Loaded from OneDrive", "Reloading the page so everything shows the loaded copy.", [{ label: "OK", value: 1, primary: true }]);
      location.reload();
    } else {
      S.etag = info ? info.etag : null; S.conflict = false;
      await save(true);
    }
  }

  async function importFromDevice() {
    const bytes = await pickFile();
    if (!bytes) return;
    if (!isSqlite(bytes)) { await ask("Not a footsim.db", "That file isn't a SQLite database, so nothing was changed.", [{ label: "OK", value: 1, primary: true }]); return; }
    const c = await ask("Replace what's loaded?", "This swaps the current simulation for the file you chose, then saves it to OneDrive.",
      [{ label: "Replace and save", value: "go", primary: true }, { label: "Cancel", value: "no" }]);
    if (c !== "go") return;
    await backend.importDb(bytes);
    backend.markDirty();
    if (S.mode === "cloud" && tok) await save(true);
    if (!backend.dirty) location.reload();                 // saved: reload so every screen shows the new file
    else if (typeof refresh === "function") refresh();     // not saved yet: keep it in this tab and show it
  }

  async function download() {
    const bytes = await backend.exportDb();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
    a.download = "footsim.db"; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  async function signOut() {
    if (backend.dirty) {
      const c = await ask("Sign out with unsaved changes?", "They will be lost unless you save first.",
        [{ label: "Cancel", value: "no", primary: true }, { label: "Sign out anyway", value: "out" }]);
      if (c !== "out") return;
    }
    forget(); location.reload();
  }

  function attach(be) {
    backend = be;
    const css = document.createElement("style");
    css.textContent = `.fsBar{flex-basis:100%;display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;position:relative}
      .fsStatus{color:var(--muted,#5b6660)} .fsStatus.warn{color:#8a5a00;font-weight:600} .fsStatus.bad{color:#b3261e;font-weight:600}
      .fsBar button{padding:6px 12px} .fsMenu{margin-left:auto}
      .fsPanel{display:none;position:absolute;right:0;top:100%;z-index:50;margin-top:6px;background:var(--panel,#fff);border:1px solid var(--line,#d8d4c8);border-radius:10px;padding:8px;min-width:230px;box-shadow:0 8px 24px rgba(0,0,0,.15);flex-direction:column;gap:6px}
      .fsPanel.open{display:flex} .fsPanel [data-who]{color:var(--muted,#5b6660);padding:2px 4px 6px;border-bottom:1px solid var(--line,#d8d4c8)} .fsPanel button{text-align:left}`;
    document.head.appendChild(css);
    bar = document.createElement("div"); bar.className = "fsBar";
    bar.innerHTML = `<span class="fsStatus"></span><button class="fsSave">Save</button>
      <button class="fsMenu" aria-label="More" title="More">⋯</button>
      <div class="fsPanel"><div data-who></div>
        <button data-act="dl">Download a copy (footsim.db)</button>
        <button data-act="imp">Import a footsim.db from this device</button>
        <button data-act="out">Sign out</button></div>`;
    $status = bar.querySelector(".fsStatus"); $save = bar.querySelector(".fsSave"); $menu = bar.querySelector(".fsMenu"); $panel = bar.querySelector(".fsPanel");
    (document.querySelector("header") || document.body).appendChild(bar);

    $save.onclick = () => save(true);
    $menu.onclick = e => { e.stopPropagation(); $panel.classList.toggle("open"); };
    document.addEventListener("click", e => { if (!$panel.contains(e.target)) $panel.classList.remove("open"); });
    $panel.onclick = e => {
      const a = e.target.dataset && e.target.dataset.act; if (!a) return;
      $panel.classList.remove("open");
      if (a === "dl") download(); else if (a === "imp") importFromDevice(); else if (a === "out") signOut();
    };
    backend.onDirty(render);
    window.addEventListener("beforeunload", e => { if (backend.dirty) { e.preventDefault(); e.returnValue = ""; } });
    setInterval(() => save(false), C.autosaveMinutes * 60 * 1000);
    window.footsimCloud.state = S;
    render();
    if (S.firstSave && S.mode === "cloud") save(true);   // first run: put the starting file on OneDrive straight away
  }

  window.footsimCloud = { prepare, attach, save: () => save(true), state: S, config: C };
})();
