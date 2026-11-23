// Runs the Python engine (Pyodide) off the main thread so the page stays responsive on a phone.
// Talks to engine.js with {id, op, args} messages; replies {id, ok, result|error}. Progress: {progress: "text"}.
let py = null, handle = null;

const progress = text => postMessage({ progress: text });
const DB = "/app/footsim.db";

async function fetchBytes(url, what) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Couldn't download ${what} (${r.status})`);
  return new Uint8Array(await r.arrayBuffer());
}

const ops = {
  async init({ pyodideBase, bundleUrl, seedUrl, existingDb }) {
    progress("Loading Python (first time only is slow)…");
    importScripts(pyodideBase + "pyodide.js");
    py = await loadPyodide({ indexURL: pyodideBase });
    progress("Loading database support…");
    await py.loadPackage("sqlite3");
    progress("Loading the simulator…");
    const bundle = await fetchBytes(bundleUrl, "the simulator code");
    py.unpackArchive(bundle, "zip", { extractDir: "/app" });
    if (existingDb) py.FS.writeFile(DB, existingDb);
    else { progress("Loading World Cup history…"); py.FS.writeFile(DB, await fetchBytes(seedUrl, "the World Cup data")); }
    progress("Starting…");
    py.runPython("import sys\nsys.path.insert(0, '/app')\nfrom footsim import dispatch\ndispatch.start()");
    handle = py.runPython("dispatch.handle_json");
    return true;
  },
  call({ method, path, body }) {
    const out = handle(method, path, body == null ? "" : JSON.stringify(body));
    const [status, data] = JSON.parse(out);
    return { status, data };
  },
  exportDb() {
    return py.FS.readFile(DB);          // rollback-journal mode: the file is whole after every request
  },
  importDb({ bytes }) {
    py.FS.writeFile(DB, bytes);
    py.runPython("dispatch.start()");   // drops caches, adds any newer tables, brings branches up to date
    return true;
  },
};

self.onmessage = async e => {
  const { id, op, args } = e.data;
  try {
    const result = await ops[op](args || {});
    const transfer = result instanceof Uint8Array ? [result.buffer] : [];
    postMessage({ id, ok: true, result }, transfer);
  } catch (err) {
    postMessage({ id, ok: false, error: String(err && err.message || err) });
  }
};
