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
