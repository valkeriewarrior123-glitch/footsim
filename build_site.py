"""Build the static site (GitHub Pages) that runs the simulator in the browser.

    python build_site.py                      # writes ./site
    python build_site.py --out docs --pyodide-base https://cdn.jsdelivr.net/pyodide/v0.27.7/full/

The site holds: index.html + engine.js + engine-worker.js (the page), footsim-bundle.zip (the Python
code and data packs) and footsim-seed.db (real World Cup history with no branches). Your own saved
footsim.db is never put in the site: it lives on OneDrive.
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/"


def make_bundle(dest: Path) -> None:
    """footsim/*.py + schema.sql + data_packs, with fixed timestamps so the file only changes when content does."""
    files = [p for p in sorted((ROOT / "footsim").glob("*")) if p.suffix in (".py", ".sql")]
    files += [p for p in sorted((ROOT / "data_packs").rglob("*")) if p.is_file()]
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            info = zipfile.ZipInfo(str(p.relative_to(ROOT)).replace("\\", "/"), (2022, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())


def check_seed(db: Path, allow_branches: bool) -> None:
    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    conn.close()
    if n and not allow_branches:
        raise SystemExit(f"{db} contains {n} branch(es). The site's starting file must be plain history, so "
                         "build from a fresh database (see how-to-run.md), or pass --allow-branches.")


def digest(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()[:10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("--db", type=Path, default=ROOT / "footsim.db")
    ap.add_argument("--pyodide-base", default=PYODIDE, help="folder holding pyodide.js (a CDN or a copy in the site)")
    ap.add_argument("--allow-branches", action="store_true")
    ap.add_argument("--client-id", default="", help="Azure app registration (client) ID; leave out to build without sign-in")
    ap.add_argument("--tenant", default="common", help="Directory (tenant) ID, or 'common' / 'consumers'")
    ap.add_argument("--authority", default="", help="full sign-in URL; overrides --tenant")
    ap.add_argument("--redirect-uri", default="", help="override the page address Microsoft sends you back to")
    ap.add_argument("--graph", default="", help="override the Microsoft Graph address (testing only)")
    ap.add_argument("--folder", default="FootSim", help="subfolder of the OneDrive app folder")
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"{args.db} not found. Run the importer first.")
    check_seed(args.db, args.allow_branches)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    web = ROOT / "footsim" / "web"
    for name in ("engine.js", "engine-worker.js", "cloud.js", "manifest.webmanifest"):
        shutil.copy(web / name, out / name)
    shutil.copytree(web / "icons", out / "icons", dirs_exist_ok=True)      # home-screen icons
    make_bundle(out / "footsim-bundle.zip")
    shutil.copy(args.db, out / "footsim-seed.db")
    version = digest(out / "footsim-bundle.zip", out / "footsim-seed.db", out / "engine.js", out / "engine-worker.js", out / "cloud.js")

    cfg = {"pyodideBase": args.pyodide_base, "bundle": f"footsim-bundle.zip?v={version}",
           "seed": f"footsim-seed.db?v={version}", "worker": f"engine-worker.js?v={version}", "version": version}
    if args.client_id:
        cloud = {"clientId": args.client_id, "folder": args.folder,
                 "authority": args.authority or f"https://login.microsoftonline.com/{args.tenant}"}
        if args.redirect_uri:
            cloud["redirectUri"] = args.redirect_uri
        if args.graph:
            cloud["graph"] = args.graph
        cfg["cloud"] = cloud
    cloud_tag = f'\n<script src="cloud.js?v={version}"></script>' if args.client_id else ""
    boot = f'<script>window.FOOTSIM_CONFIG={json.dumps(cfg)};</script>{cloud_tag}\n<script src="engine.js?v={version}"></script>'
    page = (web / "index.html").read_text(encoding="utf-8")
    if "<!--FOOTSIM_BOOT-->" not in page:
        raise SystemExit("index.html is missing the <!--FOOTSIM_BOOT--> marker")
    (out / "index.html").write_text(page.replace("<!--FOOTSIM_BOOT-->", boot), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")        # GitHub Pages: serve files as they are
    print(f"Built {out}  (version {version})")
    for p in sorted(out.iterdir()):
        print(f"  {p.name:22} {'' if p.is_dir() else format(p.stat().st_size, ',') + ' bytes':>16}")


if __name__ == "__main__":
    main()
