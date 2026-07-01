# Troubleshooting Guide

This document captures high-signal debugging notes learned through real deployments. Read it before attempting changes to the dashboard or proxy.

---

## Dashboard Behind nginx `/copilot/` — "Blank Page / DB ✗" Bug

**Symptom:** Dashboard loads HTML but shows an empty page with a red "DB ✗" badge in the top right corner. No live data appears in any panel.

**Root Cause (dual-layer):** The dashboard is served behind nginx ingress at `/copilot/` path prefix. Two independent bugs prevented JavaScript from fetching data:

### Bug 1 — JavaScript fetch() calls had hardcoded absolute paths

The JavaScript used `fetch('/stats')`, `fetch('/api/stats/summary?days=1')` etc. When the browser loads the page at `https://susmannet.duckdns.org/copilot/`, these absolute paths hit nginx as `/stats` and `/api/stats/summary` — NOT `/copilot/stats`. Nginx has no route for bare `/stats` in the susmannet server block, so it returns 404 or hits a wrong upstream.

**Fix:** Added a `__bp` base path variable to all fetch calls:
```javascript
const __bp = typeof window.__BASE_PATH !== 'undefined' ? window.__BASE_PATH : '';
// Now ALL fetch calls use __bp prefix:
fetch(__bp + '/stats')        // → /copilot/stats when behind nginx
fetch(__bp + '/v1/models')    // → /copilot/v1/models
fetch(__bp + '/api/history?days=1&limit=5')  // → /copilot/api/history
```

The value of `window.__BASE_PATH` is injected by serve.py at HTML render time:
```python
injection = f'<script>window.__BASE_PATH="{PROXY_PATH_PREFIX}";</script>'
```

### Bug 2 — serve.py path normalization didn't handle query strings

Even after nginx strips `/copilot/` prefix and proxies to serve.py (port 3002), the request path still included query parameters. For example, `self.path = "/api/stats/summary?days=1"` would fail the check `self.path.startswith("/api/")` because... wait, actually it WOULD match. The real issue was that when nginx `proxy_pass http://gcopilot-dash/` strips `/copilot/`, it passes the remaining path to serve.py. But when served via direct port 3002 AND the browser JS prepends `/copilot/`, serve.py needed to normalize:

```python
# BEFORE (broken — query strings broke prefix matching)
if self.path.startswith(prefix):        # "/api/stats/summary?days=1" → works BUT
    _proxy_to_upstream(self, self.path)  # sends full path with ? correctly

# AFTER (robust — normalizes path before any comparison)
_norm_path = self.path.split("?")[0]  # remove query string for routing
if PROXY_PATH_PREFIX and _norm_path.startswith(PROXY_PATH_PREFIX):
    _norm_path = _norm_path[len(PROXY_PATH_PREFIX):] or "/"
for prefix in API_PREFIXES:
    if _norm_path.startswith(prefix):
        _proxy_to_upstream(self, "/" + _norm_path.lstrip("/"))
```

### Environment Variable Reference

| Env Var | Value on Databricks | Purpose |
|---|---|---|
| `PROXY_BACKEND` | `http://192.168.86.39:8001` | Upstream DGX Spark proxy for server-side forwarding |
| `PROXY_PATH_PREFIX` | `/copilot` | Injected into HTML so browser JS prefixes fetch URLs |
| `DASHBOARD_PORT` | `3002` | Listen port (nginx proxies to this) |
| `HTML_PROXY_URL` | empty | Kept empty so browser calls hit serve.py → proxies server-side |

### How to Verify the Fix Works

After deploying, run these tests from inside the Databricks machine:

```bash
# 1. Container is running
docker inspect gcopilot-dashboard --format '{{.State.Status}}'  # should be "running"

# 2. serve.py has path normalization
docker exec gcopilot-dashboard grep '_norm_path' /srv/serve.py | head -1

# 3. HTML injects __BASE_PATH correctly
curl -s http://localhost:3002/ | grep '__BASE_PATH'

# 4. ALL fetch calls use __bp variable (should be >=11)
docker exec gcopilot-dashboard grep -c 'fetch(__bp' /srv/index.html

# 5. Test live data endpoints via nginx with correct Host header
curl -sk https://127.0.0.1/copilot/stats \
  -H "Host: susmannet.duckdns.org" | python3 -c "import sys,json;print(json.load(sys.stdin).get('success_count','FAIL'))"

curl -sk https://127.0.0.1/copilot/api/stats/summary?days=1 \
  -H "Host: susmannet.duckdns.org" | python3 -c "import sys,json;print(json.load(sys.stdin).get('total_requests','FAIL'))"
```

**Expected:** Both commands return positive numbers (not "FAIL").

### Why This Took So Long to Fix

1. **Testing confusion:** When testing from the Databricks host itself, `curl http://localhost:3002/stats` worked because port 3002 directly serves data. The bug only manifested through nginx `/copilot/` path where fetch calls needed the prefix.

2. **Docker image caching:** After fixing local files, the Docker container on Databricks used cached layers. Rebuilding with `docker build -f dashboard/Dockerfile.deploy -t gcopilot-dashboard .` was required AND the old container had to be stopped/removed before running the new image.

3. **Two-layer dependency:** Both fixes (JS __bp prefix AND serve.py _norm_path) were needed simultaneously. Fixing one without the other gave partial results that looked like nothing changed.

### How to Avoid Regressing

- **Rule 1:** When modifying dashboard JS fetch endpoints, ALWAYS use `__bp + '/path'` pattern — never hardcode absolute paths
- **Rule 2:** When modifying serve.py routing, ALWAYS normalize path first: `_norm_path = self.path.split("?")[0]`
- **Rule 3:** After deploying to Databricks, ALWAYS rebuild the Docker image and restart the container — SCP alone does NOT update a running container
- **Rule 4:** Test via nginx Host header (`curl -H "Host: susmannet.duckdns.org"`) not just direct port

---

## Deployment Workflow for Databricks Dashboard

### Correct Steps (in order)

1. Edit local files: `dashboard/index.html`, `dashboard/serve.py`
2. SCP to Databricks host
3. Rebuild Docker image: `docker build -f dashboard/Dockerfile.deploy -t gcopilot-dashboard .`
4. Stop & remove old container: `docker stop gcopilot-dashboard && docker rm gcopilot-dashboard`
5. Run new container with correct config:
   ```bash
   docker run -d --name gcopilot-dashboard \
     --restart unless-stopped \
     --network docucraft_docucraft-network \
     -p 3002:3002 \
     -e PROXY_BACKEND=http://192.168.86.39:8001 \
     -e DASHBOARD_PORT=3002 \
     -e PROXY_PATH_PREFIX=/copilot \
     gcopilot-dashboard
   ```
6. Validate via nginx /copilot/ path prefix (see verification above)

### Common Mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Wrong network (`--network host` vs `docucraft_docucraft-network`) | Container can't reach nginx Docker DNS name | Use correct network |
| Wrong env var (`PROXY_URL` vs `PROXY_BACKEND`) | 502 error, "PROXY_BACKEND not configured" | Always use `PROXY_BACKEND` for server-side proxying |
| Missing `PROXY_PATH_PREFIX=/copilot` | HTML has empty __BASE_PATH, fetch hits wrong paths | Set env var in docker run |
| Not rebuilding Docker image after SCP | Container runs old code with bugs | Always rebuild + restart |

---

## Dashboard Shows Zero Data After Login (Root Cause: Malformed `<script>` Tag)

**Symptom:** Login works (session cookie set, `/copilot/` returns 200), and every backend API endpoint verified via `curl` returns correct, non-empty JSON — but the browser-rendered dashboard shows completely blank panels, as if no JavaScript ever ran.

**Root Cause:** `dashboard/index.html` had its single main inline `<script>` (opened once, near the top of `<body>`) accidentally left **unclosed**. A second, stray `<script>...</script>` pair was appended near the very end of the file (originally meant to render the build-version badge). Per the HTML spec, `<script>` is a "raw text" element — the browser's HTML tokenizer does **not** recognize nested `<script>` tags; it treats everything as literal text until the **first** occurrence of the literal string `</script>`. That means:

- The *entire* body of the intended main script, PLUS the literal text `<script>` from the stray second tag, PLUS everything up to the second tag's `</script>`, was all parsed as **one giant script body**.
- The literal text `<script>` embedded inside that body is invalid JavaScript syntax (`SyntaxError: Unexpected token '<'`).
- A syntax error in an inline `<script>` means the **entire script fails to parse and none of it executes** — not just the erroneous tail. Every function (`refreshLive()`, `checkDb()`, chart rendering, tab click handlers, etc.) silently never ran.
- Because parsing failure happens client-side, in the browser, `curl`-based verification of the backend API always looked perfectly healthy — there is no way to catch this bug from the server side alone.

**How to detect it:**
```powershell
# Count plain (no-src) <script> tags — there must be exactly ONE for the main script
# (the two CDN <script src=...></script> tags in <head> are fine, they always self-close)
Select-String -Path dashboard/index.html -Pattern '<script'

# Extract the body between the first '<script>' and the first '</script>' and syntax-check it
python -c "
import re
html = open('dashboard/index.html', encoding='utf-8').read()
start = html.index('<script>\n')
end = html.index('</script>', start)
open('_extracted.js','w',encoding='utf-8').write(html[start+len('<script>'):end])
"
node --check _extracted.js   # must print nothing / exit 0
```

**How to avoid regressing:**
- **Rule:** `dashboard/index.html` must have exactly ONE plain `<script>` tag (no `src=` attribute) for the entire application. Any new inline JS (e.g. a version badge, a one-off snippet) must be appended as plain statements *inside* the existing script body — never as a new `<script>...</script>` pair.
- Before every dashboard deploy, run the detection snippet above (or equivalent) to catch this class of bug automatically. Consider wiring it into `scripts/deploy_dashboard.py` as a pre-flight check that aborts the deploy if it fails.
- Defensively, `dashboard/index.html`'s bootstrap sequence (`restorePanelOrder()`, `initTpsChart()`, `checkDb()`, `refreshLive()`, `updateVramModels()`) is now wrapped in a `safeBoot()` helper so that if any ONE of these throws at runtime (e.g. the Chart.js CDN is unreachable), the others still run — data will still populate even if one chart fails to render. This does not protect against a syntax error breaking the whole script (nothing can), but it does protect against runtime exceptions cascading.

---

## Copilot CLI Extensions ("skills") Silently Broken by Corrupted Text / Fragile Shell Quoting

**Symptom:** `.github/extensions/*/extension.mjs` tools (`run-system-tests`, `test-auth`, `deploy-proxy`, etc.) fail to load, or load but every SSH-based check fails/returns garbage, even though the underlying services are healthy.

**Root Cause #1 — invalid JS identifiers.** A prior uncommitted session's find-replace corrupted the literal string `Databricks`/`databricks` into `"databrick (local home server hostname is databrick)"` across ~10 files, including as bare JS identifiers inside both extension files (e.g. `const databrick (local home server hostname is databrick) = "..."`). This is invalid JavaScript syntax — the whole extension file fails to load. **Detection:** `node --check .github/extensions/<name>/extension.mjs` must exit 0 for every extension file. Run this after ANY edit to an extension.

**Root Cause #2 — broken SSH command quoting on Windows.** Both extensions had a hand-rolled `ssh(host, cmd)` helper that built a single shell-command string (e.g. `` ssh host "curl ... -H \"Authorization: Bearer KEY\" ..." ``) and executed it via `pwsh -Command <string>` (on Windows) or `bash -c <string>` (on POSIX). This is fragile:
- PowerShell double-quoted strings do **not** treat `\"` as an escaped quote (only `` `" `` backtick-quote works) — a literal `\"` inside a pwsh `-Command` string silently truncates the string early, corrupting/breaking the remote command.
- The equivalent bash-style `'\''` single-quote-escaping trick used in the other extension is also not valid PowerShell single-quoted string syntax.
- Because this only breaks on Windows (bash correctly handles the escaping), it went undetected if the extension was ever tested on a Linux CI runner but not on the maintainer's actual Windows workstation.

**Fix:** `ssh()` in both extensions now calls `execFile("ssh", ["-o", "ConnectTimeout=10", host, cmd])` directly with `cmd` as one argv element — Node passes it to the local `ssh` binary without any intermediate shell re-parsing, and `ssh` forwards it byte-for-byte to the remote host's shell. No local quoting/escaping is needed at all.

**Root Cause #3 — credentials env vars not set when the CLI is launched.** Both extensions read `process.env.COPILOT_PROVIDER_API_KEY`, `DASHBOARD_PASSWORD`, `ADMIN_PASSWORD`, etc. These are normally injected by `copilot-dgx.bat` before launching the CLI. If the CLI is launched any other way (plain `copilot`, a CI runner, another agent's shell), these are unset/empty and every auth-dependent check reports a false "regression" (e.g. "Valid key → 200" reports 401).

**Fix:** Both extensions now call a `loadDotEnvFallback()` function at startup that reads the repo-root `.env` file directly (via `fileURLToPath(import.meta.url)` relative pathing) and populates any of these vars that aren't already set in `process.env` — never overriding a real env var that IS set. This makes the tools work correctly and reproducibly regardless of how the CLI session was started.

**How to verify extensions are healthy after any change:**
```
# Inside a Copilot CLI session:
extensions_reload            # reload from disk, confirm both show "ready" with no errors
run-system-tests             # should report 0 failed (currently 15/15 checks passing)
test-auth / test-dashboard / test-database / test-proxy-to-db / test-proxy-to-dgx / test-completions-api
```
If `node --check` passes but the extension still fails to appear as "ready" in `extensions_reload`'s output, use `extensions_manage` with `operation: "inspect"` to view its log file for the real error.

---

## Test-Script Bugs That Made Healthy Endpoints Look Broken (False Regressions)

Several checks inside `.github/extensions/system-tests/extension.mjs` encoded incorrect assumptions about API response shapes, and reported failures on endpoints that were actually working correctly:

- **Bare-array assumption:** `/api/usage/daily`, `/api/usage/hourly`, `/api/history`, and `/api/models/enriched` all return `{"count": N, "data": [...]}` (or `{"data": [...], "last_refresh": ...}`), never a bare top-level array. Checks like `Array.isArray(JSON.parse(body))` will ALWAYS be `false` against the real API. **Fix:** always check `Array.isArray(parsed?.data)`.
- **Non-existent `/health.mongo` field:** `/health` (proxy/main.py) never returned a `mongo` field. The real signal is `mongodb_enabled` on the admin-only `GET /api/admin/status` endpoint (requires HTTP Basic auth via `ADMIN_USERNAME`/`ADMIN_PASSWORD`). **Fix:** added a shared `mongoEnabled()` helper that queries the correct endpoint.
- **"History count grows" flaky on a busy system:** comparing raw `data.length` before/after an inference request fails once the collection already has more entries than the endpoint's default page `limit` (200) — the count is capped at 200 on both sides and never visibly "grows". **Fix:** also accept the check as passing if the *newest* record (`data[0]`, sorted descending by timestamp) has the expected `model` and a `timestamp` within the last ~20 seconds — this proves the write pipeline works even when the raw count can't grow further.
- **Streaming SSE check always failed:** the check asserted `!out.includes('"content":""')`, but a healthy SSE stream's *final* chunk legitimately has empty content (paired with `finish_reason`). This assertion was backwards and failed on every valid response. **Fix:** check for `data: [DONE]` (or a `finish_reason`) AND at least one chunk with non-empty content, using a regex that specifically excludes the empty-string case.
- **`ADMIN_PASS.replace(/!/g, "\\!")` corrupted the real password on Windows:** this bash-history-expansion escape trick is meaningless (and harmful) when the command runs through `pwsh -Command` locally — it turned `susmannet1!` into a literal backslash + `!`, which never matched, causing "Admin valid auth → 200" to report 401. **Fix:** removed the escape entirely (neither `pwsh -Command` nor `bash -c` perform history expansion in non-interactive/script mode, so no escaping was ever needed).

**Lesson:** when a test script disagrees with a manually-verified `curl` result, trust the `curl` result and treat the test as the bug — don't assume a "regression" without checking the real API response shape first.

---

## `float('inf')` in `/stats` and `/api/history` — Silent 500 Errors From MongoDB-Persisted Documents

**Symptom:** `/stats` or `/api/history` intermittently (or consistently, once a bad document exists) returns HTTP 500 with body `Internal Server Error`. Proxy logs show:
```
ValueError: Out of range float values are not JSON compliant: inf
```

**Root Cause:** `proxy/tracker.py`'s `RequestStats.streaming_tps` computed a real-time tokens-per-second value from `(last_token_time - first_token_time)`. When a completion finished in under 10ms (very common for small/fast models like `qwen3:4b` with a short `max_tokens`), it deliberately returned `float('inf')` as a sentinel. Starlette's `JSONResponse` calls `json.dumps(..., allow_nan=False)` by default, which raises `ValueError` on `inf`/`-inf`/`NaN` — crashing the whole endpoint. Worse, this `inf` value was also being **persisted to MongoDB** (BSON doubles support `Infinity` even though JSON does not), so once a single bad document existed, `/api/history` would 500 on *every* subsequent request that included it in the result page — permanently, until the document was fixed.

**Fix (`proxy/tracker.py`):** `streaming_tps` now falls back to `avg_completion_tps` (the same finite fallback already used when timestamps are missing) instead of returning `float('inf')` for the very-fast-completion case.

**One-time data repair required after this fix:** any documents written by the OLD buggy code before the fix was deployed will still contain `Infinity`, and must be repaired directly in MongoDB (deploying the code fix alone does not retroactively clean existing bad data):
```python
# Run inside the gcopilot-proxy container (has motor/pymongo + MONGO_URI available)
import asyncio, math, os
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    client = AsyncIOMotorClient(os.environ["MONGO_URI"])
    db = client[os.environ.get("MONGO_DB", "radiacode")]
    bad_ids = []
    async for d in db.requests.find({}):
        if any(isinstance(v, float) and not math.isfinite(v) for v in d.values()):
            bad_ids.append(d["_id"])
    if bad_ids:
        await db.requests.update_many({"_id": {"$in": bad_ids}}, {"$set": {"tps": 0.0, "ttft_ms": 0.0}})

asyncio.run(main())
```
Note: a MongoDB query like `db.requests.find({"tps": float("inf")})` does **not** reliably match `Infinity`-valued documents — you must scan documents and check `math.isfinite()` on each numeric field in application code, as shown above.

**How to avoid regressing:** any code path that computes a rate (`tokens / elapsed_time`) must clamp or fall back before the value can reach `inf` or `NaN` — never rely on downstream JSON serialization to catch it, because by the time it's serialized, the value may already be persisted to the database.

---

## Quick Health Check — One Command, No Setup Required

The fastest, most reliable way for **any** agent (human or AI, regardless of model capability) to verify the whole stack after making changes:

```
run-system-tests
```

This is a Copilot CLI extension tool (from `.github/extensions/system-tests/`) — it requires no arguments, no manual curl commands, and no memorized endpoint list. It automatically reads credentials from the repo-root `.env` file if they aren't already in the environment (see "credentials env vars not set" above), so it works the same whether launched via `copilot-dgx.bat`, a plain `copilot` session, or an automated agent.

As of this writing it runs **15 checks across 6 subsystems** (Auth, Completions API, Dashboard, Database, DGX Spark Connection, DB Write Pipeline) and all 15 pass on a healthy stack. A single ❌ in the output points directly at the broken subsystem — read the failing line's label, it names the exact endpoint/check that failed.

For deeper, subsystem-specific diagnosis, six more focused tools are available from the same extension: `test-auth`, `test-dashboard`, `test-database`, `test-proxy-to-dgx`, `test-proxy-to-db`, `test-completions-api`, plus `test-all-models` for a full model-by-model inference sweep. All are self-contained — just invoke them, no setup.

If any extension tool is missing entirely (not just failing), run `extensions_reload` first — it's possible the extension failed to load due to a JS syntax error (see "Copilot CLI Extensions Silently Broken" above); `extensions_manage` with `operation: "inspect"` shows the failure reason.

---

## DGX Spark Proxy Deployment

The proxy deploys to DGX Spark independently of the dashboard. Use `python scripts/deploy.py` from Windows repo root.

- The proxy is containerized as `gcopilot-proxy` with `--network host` on port 8001
- Ollama runs as systemd service, auto-discovers models every 30 seconds
- MongoDB lives on separate machine (`192.168.86.48:27017`)
- The proxy is fully dynamic — no code changes needed to add/remove models

### Quick Reference

```bash
# Deploy proxy to DGX Spark
python scripts/deploy.py

# Validate proxy health
curl http://192.168.86.39:8001/health

# Check running models in VRAM
curl http://192.168.86.39:8001/api/models/running

# Restart container (after code changes)
ssh dgxspark "sudo docker restart gcopilot-proxy"
```

---

## Architecture Summary

```
Browser at https://susmannet.duckdns.org/copilot/
         |
         v
nginx on Databricks (192.168.86.48, port 443)
  - susmannet.duckdns.org server block at /copilot/
  - proxy_pass http://gcopilot-dash/ (strips /copilot/ prefix)
         |
         v
serve.py on gcopilot-dashboard container (port 3002, docucraft_docucraft-network)
  - Injects window.__BASE_PATH = "/copilot" into HTML
  - Normalizes paths, proxies API requests to upstream
         |
         v
DGX Spark proxy at http://192.168.86.39:8001 (gcopilot-proxy container)
  - Real-time stats, model list, cost engine, MongoDB persistence
```
