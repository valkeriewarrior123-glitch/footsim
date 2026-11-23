// In-browser backend for the static (GitHub Pages) build. Defines window.footsimBackend:
//   ready                      promise: resolves when the Python engine and database are loaded
//   call(method, path, body)   -> {status, data}   same routes as the local server
//   exportDb() / importDb(u8)  read / replace the whole footsim.db (used for saving and loading)
//   dirty, onDirty(fn)         true once something changed since the last markClean()
// Configuration comes from window.FOOTSIM_CONFIG, written by build_site.py.
(() => {
  const cfg = Object.assign({ pyodideBase: "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/", bundle: "footsim-bundle.zip",
    seed: "footsim-seed.db", worker: "engine-worker.js" }, window.FOOTSIM_CONFIG || {});
  const abs = u => new URL(u, document.baseURI).href;

  // ---- loading overlay
  const box = document.createElement("div");
  box.style.cssText = "position:fixed;inset:0;z-index:100;display:grid;place-items:center;background:var(--bg,#f4f2ec);color:var(--ink,#1d2320);font:15px system-ui,sans-serif;text-align:center;padding:24px";
  box.innerHTML = '<div><div style="font-size:22px;font-weight:700;margin-bottom:10px">Foot<span style="color:#1f6b45">Sim</span></div><div id="fsMsg">Starting…</div><div id="fsErr" style="color:#b3261e;margin-top:12px;max-width:34em"></div></div>';
  document.body.appendChild(box);          // this script is loaded at the end of <body>
  const say = t => { const m = box.querySelector("#fsMsg"); if (m) m.textContent = t; };

  // ---- worker plumbing
  const worker = new Worker(abs(cfg.worker));
  const pending = new Map();
  let nextId = 1;
  worker.onmessage = e => {
    if (e.data.progress) return say(e.data.progress);
    const p = pending.get(e.data.id);
    if (!p) return;
    pending.delete(e.data.id);
    e.data.ok ? p.resolve(e.data.result) : p.reject(new Error(e.data.error));
  };
  worker.onerror = e => { box.querySelector("#fsErr").textContent = "The engine stopped: " + (e.message || "unknown error"); };
  const send = (op, args, transfer) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, op, args }, transfer || []);
  });

  // ---- public API
  const listeners = [];
  const backend = {
    dirty: false,
    rev: 0,                                   // counts changes, so a save can tell if edits arrived while it was uploading
    onDirty(fn) { listeners.push(fn); },
    markClean() { backend.dirty = false; listeners.forEach(f => f(false)); },
    markDirty() { backend.rev++; backend.dirty = true; listeners.forEach(f => f(true)); },
    async call(method, path, body) {
      const r = await send("call", { method, path, body });
      // Rolling in the editor is only a preview until it is saved, so it doesn't count as a change.
      const changes = method !== "GET" && !(method === "POST" && /^\/api\/roll\//.test(path)) && r.status < 400;
      if (changes) backend.markDirty();
      return r;
    },
    exportDb() { return send("exportDb"); },
    async importDb(bytes) { await send("importDb", { bytes }, [bytes.buffer]); backend.markClean(); },
  };
  window.footsimBackend = backend;

  // cloud.js (when configured) signs in and hands over the database saved on OneDrive; otherwise we start from the seed.
  const cloud = window.footsimCloud;
  backend.ready = Promise.resolve(cloud ? cloud.prepare() : window.FOOTSIM_EXISTING_DB || null).then(existing => send("init", {
    pyodideBase: abs(cfg.pyodideBase), bundleUrl: abs(cfg.bundle), seedUrl: abs(cfg.seed), existingDb: existing || null,
  })).then(() => { box.remove(); if (cloud) cloud.attach(backend); }).catch(err => {
    box.querySelector("#fsErr").textContent = "Couldn't start: " + err.message;
    throw err;
  });
})();
