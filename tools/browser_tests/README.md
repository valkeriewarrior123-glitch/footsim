# Browser checks (not part of `unittest`)

These drive the built site in headless Chrome with Python Playwright (viewport 390x844).

- `editor_flow.py <url>`: new branch, roll + save, knockouts, stats, changes, create player, export.
- `cloud_flow.py`: sign-in, first save, Save, reload, newer-copy conflict, 15-minute auto-save, expired sign-in,
  download, local mode. Needs the fake Microsoft/OneDrive server and a test build:

```
python tools/browser_tests/mock_ms.py &                      # fake sign-in + OneDrive on :8803 (restart it between runs)
python build_site.py --out /tmp/site2 --pyodide-base pyodide/ --client-id test-client \
   --authority http://localhost:8803/tenant --graph http://localhost:8803/graph
# put a copy of Pyodide in /tmp/site2/pyodide (see claude/how-to-run.md), then:
cd /tmp/site2 && python -m http.server 8802 &
python tools/browser_tests/cloud_flow.py
```
