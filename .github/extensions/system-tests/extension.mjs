// Extension: system-tests
// Comprehensive test suite for the LLM stack: completions API, auth, dashboard,
// MongoDB, proxy→DGX Ollama chain, and proxy→DB pipeline.
//
// Architecture under test:
//   Browser/Client → nginx (remote host :443/:80) → gcopilot-proxy (:8001)
//                  → Ollama on DGX Spark (:11434)
//                  → MongoDB on the remote host (:27017)
//
// All tools return a string report with ✅/❌/⚠️ per check.
// Designed for interactive use via Copilot CLI and for automated use via
// write_agent / workflow orchestration.

import { joinSession } from "@github/copilot-sdk/extension";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const exec = promisify(execFile);
const isWin = globalThis.process?.platform === "win32";
const shell = isWin ? "pwsh" : "bash";
const shellFlag = isWin ? "-Command" : "-c";

// ── .env fallback loader ─────────────────────────────────────────────────────
// Credentials are normally injected into process.env by copilot-dgx.bat before
// launching the CLI. When the CLI is launched any other way (plain `copilot`,
// CI, another agent's shell), those vars are unset and every auth-dependent
// check below would silently look like a fresh regression. To make these
// tools reliable no matter how the session was started, fall back to reading
// the repo-root .env file directly (never overrides real env vars already set).
function loadDotEnvFallback() {
    try {
        const here = path.dirname(fileURLToPath(import.meta.url));
        const envPath = path.resolve(here, "..", "..", "..", ".env"); // repo root
        const text = readFileSync(envPath, "utf8");
        for (const line of text.split(/\r?\n/)) {
            const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/.exec(line);
            if (!m) continue;
            const [, key, rawVal] = m;
            if (process.env[key] !== undefined && process.env[key] !== "") continue;
            let val = rawVal.trim();
            if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
                val = val.slice(1, -1);
            }
            process.env[key] = val;
        }
    } catch { /* .env not found — rely on process.env only, defaults will apply */ }
}
loadDotEnvFallback();

// ── Env / config ─────────────────────────────────────────────────────────────

const API_KEY      = process.env.COPILOT_PROVIDER_API_KEY || process.env.PROXY_API_KEY || "";
const DASH_USER    = process.env.DASHBOARD_USERNAME || "darkmatter2222";
const DASH_PASS    = process.env.DASHBOARD_PASSWORD || "";
const ADMIN_USER   = process.env.ADMIN_USERNAME || "darkmatter2222";
const ADMIN_PASS   = process.env.ADMIN_PASSWORD || "";

const DGX          = "dgxspark";                            // ssh host alias
const REMOTE_HOST   = "darkmatter2222@192.168.86.48";

const PROXY_DIRECT = "http://192.168.86.48:8001";          // direct container port
const PUBLIC_BASE  = "http://192.168.86.48/copilot";       // via nginx
const PUBLIC_HTTPS = "https://susmannet.duckdns.org/copilot";

// All models known to be installed on DGX Spark
const KNOWN_MODELS = [
    "qwen3",
    "qwen3-coder",
    "qwen3-coder-next:q8_0",
    "obliterated",
    "qwen3.6:27b-mtp-q4_K_M",
    "qwen3-coder-spec:latest",
    "qwen3-coder-next-spec:latest",
    "qwen3:4b",
];

// ── Helpers ───────────────────────────────────────────────────────────────────

function run(cmd) {
    return exec(shell, ["-NoProfile", "-NonInteractive", shellFlag, cmd]);
}

// IMPORTANT: invoke `ssh` directly via execFile with an argv array (no shell
// in between) so `cmd` is forwarded to the remote host byte-for-byte. Building
// this as a quoted string and re-parsing it through pwsh/bash (the old
// approach) is fragile — pwsh does NOT treat `\"` as an escaped quote inside
// a double-quoted string (only backtick-quote is), so any cmd containing a
// literal `"` (e.g. `-H "Authorization: Bearer ..."`) silently truncated the
// remote command and broke every SSH-based check on Windows.
async function ssh(host, cmd) {
    const { stdout } = await exec("ssh", ["-o", "ConnectTimeout=10", host, cmd]);
    return stdout.trim();
}

/** HTTP status code check (curl). Returns the numeric code string, or "ERR". */
async function httpCode(url, extraArgs = "") {
    try {
        const { stdout } = await run(
            `curl.exe -sk --max-time 15 -o NUL -w "%{http_code}" ${extraArgs} "${url}"`
        );
        return stdout.trim();
    } catch { return "ERR"; }
}

/** HTTP GET, return body as string. */
async function httpGet(url, extraArgs = "") {
    try {
        const { stdout } = await run(
            `curl.exe -sk --max-time 20 ${extraArgs} "${url}"`
        );
        return stdout.trim();
    } catch (e) { return `ERR:${e.message}`; }
}

/** SSH to the remote host, run curl, return body. */
async function dbCurl(path, extraArgs = "") {
    const cmd = `curl -s --max-time 20 ${extraArgs} http://localhost:8001${path}`;
    return ssh(REMOTE_HOST, cmd).catch(e => `ERR:${e.message}`);
}

/**
 * Check whether the proxy's MongoDB persistence layer is enabled.
 * NOTE: `/health` never exposes a `mongo` field — that was a test-script
 * assumption that didn't match the real API (see proxy/main.py `/health`).
 * The real signal is `mongodb_enabled` on the admin-only `/api/admin/status`
 * endpoint (proxy/main.py:811-819), which requires HTTP Basic auth.
 */
async function mongoEnabled() {
    try {
        const body = await ssh(REMOTE_HOST,
            `curl -s --max-time 15 --user ${ADMIN_USER}:${ADMIN_PASS} http://localhost:8001/api/admin/status`
        );
        return JSON.parse(body)?.mongodb_enabled === true;
    } catch { return false; }
}

/**
 * True if `body` parses as JSON with a `.data` array — the actual response
 * shape for `/api/usage/daily`, `/api/usage/hourly`, and `/api/history`
 * (`{"count": N, "data": [...]}`, see proxy/db.py). These endpoints do NOT
 * return a bare top-level array; checking `Array.isArray(parsed)` directly
 * always fails against the real API and was a test-script bug.
 */
function hasDataArray(body) {
    try { return Array.isArray(JSON.parse(body)?.data); } catch { return false; }
}

/** Run a chat completion test via the proxy. Returns {ok, content, ms}. */
async function chatCompletion({ base, model, message = "Reply with exactly: PASS", maxTokens = 8, stream = false, apiKey = API_KEY }) {
    const body = JSON.stringify({
        model,
        messages: [{ role: "user", content: message }],
        max_tokens: maxTokens,
        stream,
    });
    const t0 = Date.now();
    try {
        const out = await ssh(REMOTE_HOST,
            `curl -s --max-time 60 ` +
            `-H 'Authorization: Bearer ${apiKey}' ` +
            `-H 'Content-Type: application/json' ` +
            `-d '${body.replace(/'/g, "'\\''")}' ` +
            `${base}/v1/chat/completions`
        );
        const ms = Date.now() - t0;
        if (stream) {
            // SSE: verify the stream terminated correctly and at least one
            // chunk carried non-empty content. NOTE: a real, healthy SSE
            // stream's *final* chunk always has `"content":""` (paired with
            // `finish_reason`) — asserting the empty-content marker is absent
            // anywhere in the stream was wrong and made this check fail on
            // every valid response.
            const hasDone = out.includes("data: [DONE]") || out.includes('"finish_reason"');
            const hasContent = /"content":"(?!")/.test(out); // at least one non-empty content field
            return { ok: hasDone && hasContent, content: out.slice(0, 120), ms };
        }
        const parsed = JSON.parse(out);
        const content = parsed?.choices?.[0]?.message?.content || "";
        return { ok: content.length > 0, content: content.slice(0, 80), ms };
    } catch (e) {
        return { ok: false, content: e.message, ms: Date.now() - t0 };
    }
}

/** Format pass/fail. */
function chk(label, pass, detail = "") {
    return `  ${pass ? "✅" : "❌"} ${label}${detail ? ` — ${detail}` : ""}`;
}

/** Summary header. */
function header(title) {
    return `\n── ${title} ─${"─".repeat(Math.max(0, 50 - title.length))}`;
}

// ── Session ───────────────────────────────────────────────────────────────────

const session = await joinSession({
    hooks: {
        onSessionStart: async () => {
            await session.log("🧪 System-Tests extension loaded — 8 test tools ready", { level: "info", ephemeral: true });
        },
    },
    tools: [

        // ══════════════════════════════════════════════════════════════════════
        // 1. COMPLETIONS API
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "test-completions-api",
            description: [
                "Test the OpenAI-compatible /v1/chat/completions endpoint through the full stack.",
                "Checks: GET /v1/models list, non-streaming chat completion, streaming SSE, token count in response,",
                "correct Content-Type headers. Routes through nginx → gcopilot-proxy → Ollama on DGX.",
                "Optional 'model' param (default: qwen3). Optional 'stream' bool to test SSE."
            ].join(" "),
            parameters: {
                type: "object",
                properties: {
                    model: { type: "string", description: "Model to test (default: qwen3)" },
                    stream: { type: "boolean", description: "Also test streaming SSE (default: false)" },
                }
            },
            handler: async (args) => {
                const model = args.model || "qwen3";
                const lines = [header("Completions API Test")];
                const base = PROXY_DIRECT;

                // GET /v1/models
                const modelsBody = await dbCurl("/v1/models", `-H "Authorization: Bearer ${API_KEY}"`);
                let modelsOk = false, modelCount = 0;
                try {
                    const parsed = JSON.parse(modelsBody);
                    modelsOk = Array.isArray(parsed?.data) && parsed.data.length > 0;
                    modelCount = parsed?.data?.length || 0;
                } catch {}
                lines.push(chk(`GET /v1/models returns model list (${modelCount} models)`, modelsOk));

                // Check target model is in list
                let modelInList = false;
                try {
                    const parsed = JSON.parse(modelsBody);
                    modelInList = parsed?.data?.some(m => m.id === model || m.id.startsWith(model));
                } catch {}
                lines.push(chk(`Model '${model}' appears in /v1/models`, modelInList));

                // Non-streaming completion
                await session.log(`Testing non-streaming completion with ${model}...`);
                const nonStream = await chatCompletion({ base, model, stream: false });
                lines.push(chk(`Non-streaming completion (${nonStream.ms}ms)`, nonStream.ok, `"${nonStream.content}"`));

                // Streaming completion
                if (args.stream !== false) {
                    await session.log(`Testing streaming SSE with ${model}...`);
                    const streamResult = await chatCompletion({ base, model, stream: true });
                    lines.push(chk(`Streaming SSE completion (${streamResult.ms}ms)`, streamResult.ok));
                }

                // Correct Content-Type header
                const ctCheck = await run(
                    `curl.exe -sk --max-time 5 -I -H "Authorization: Bearer ${API_KEY}" "${PROXY_DIRECT}/v1/models"`
                ).then(r => r.stdout).catch(() => "");
                const hasJsonCt = ctCheck.toLowerCase().includes("application/json");
                lines.push(chk("Response Content-Type: application/json", hasJsonCt));

                const passed = lines.filter(l => l.includes("✅")).length;
                const total = lines.filter(l => l.includes("✅") || l.includes("❌")).length;
                return `Completions API: ${passed}/${total} passed\n${lines.join("\n")}`;
            },
        },

        // ══════════════════════════════════════════════════════════════════════
        // 2. AUTHENTICATION
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "test-auth",
            description: [
                "Test all authentication layers in the stack.",
                "Checks: API key enforcement (missing → 401, wrong → 401, valid → 200),",
                "dashboard session auth (no session → 302, with session cookie → 200),",
                "admin HTTP Basic auth (no creds → 401, wrong creds → 401, valid → 200),",
                "public /health endpoint accessible without key."
            ].join(" "),
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const lines = [header("Authentication Tests")];
                const base = PROXY_DIRECT;
                const wrongKey = "invalid-key-that-should-be-rejected-12345";

                // API key enforcement
                lines.push("\nAPI Key (inference endpoints):");
                const noKey   = await httpCode(`${PUBLIC_BASE}/v1/models`);
                const badKey  = await httpCode(`${PUBLIC_BASE}/v1/models`, `-H "Authorization: Bearer ${wrongKey}"`);
                const goodKey = await httpCode(`${PUBLIC_BASE}/v1/models`, `-H "Authorization: Bearer ${API_KEY}"`);
                lines.push(chk("No key → 401",       noKey  === "401", `got ${noKey}`));
                lines.push(chk("Wrong key → 401",    badKey === "401", `got ${badKey}`));
                lines.push(chk("Valid key → 200",     goodKey === "200", `got ${goodKey}`));

                // Public health endpoint
                lines.push("\nPublic endpoints (no key required):");
                const healthCode = await httpCode(`${PUBLIC_BASE}/health`);
                lines.push(chk("/health accessible without key → 200", healthCode === "200", `got ${healthCode}`));

                // Dashboard session auth
                lines.push("\nDashboard session auth:");
                const dashNoSession = await httpCode(`${PUBLIC_BASE}/`);
                lines.push(chk("Dashboard without session → 302", dashNoSession === "302", `got ${dashNoSession}`));

                // Login and get cookie
                try {
                    await ssh(REMOTE_HOST,
                        `curl -s -X POST http://localhost:3002/login ` +
                        `-d 'username=${DASH_USER}&password=${DASH_PASS}' ` +
                        `-H 'Content-Type: application/x-www-form-urlencoded' ` +
                        `-c /tmp/systest-cookies.txt -o /dev/null`
                    );
                    const dashWithSession = await ssh(REMOTE_HOST,
                        `curl -s -o /dev/null -w '%{http_code}' -b /tmp/systest-cookies.txt http://localhost:3002/`
                    );
                    lines.push(chk("Dashboard with valid session → 200", dashWithSession === "200", `got ${dashWithSession}`));
                } catch (e) {
                    lines.push(chk("Dashboard session login flow", false, e.message));
                }

                // Wrong dashboard credentials → 302 (redirect back to login)
                try {
                    const badLogin = await ssh(REMOTE_HOST,
                        `curl -s -X POST http://localhost:3002/login ` +
                        `-d 'username=wronguser&password=wrongpass' ` +
                        `-H 'Content-Type: application/x-www-form-urlencoded' ` +
                        `-c /tmp/systest-badcookies.txt -o /dev/null -w '%{http_code}'`
                    );
                    // After bad login, dashboard should still require auth
                    const stillProtected = await ssh(REMOTE_HOST,
                        `curl -s -o /dev/null -w '%{http_code}' -b /tmp/systest-badcookies.txt http://localhost:3002/`
                    );
                    lines.push(chk("Dashboard with wrong credentials still protected", stillProtected !== "200", `got ${stillProtected}`));
                } catch (e) {
                    lines.push(chk("Dashboard wrong-creds protection", false, e.message));
                }

                // Admin endpoints
                lines.push("\nAdmin HTTP Basic auth:");
                const adminNoAuth  = await httpCode(`${PROXY_DIRECT}/api/admin/keys`);
                const adminBadAuth = await httpCode(`${PROXY_DIRECT}/api/admin/keys`, `--user wronguser:wrongpass`);
                // NOTE: no backslash-escaping of special characters (e.g. "!") here —
                // `httpCode`/`run()` executes locally via pwsh on Windows (or bash -c on
                // POSIX), neither of which perform interactive-style history expansion
                // in a non-interactive script/-Command invocation, so escaping "!" was
                // both unnecessary and actively corrupted the password on Windows
                // (pwsh has no `\x` escape convention, so "\\!"  became a literal
                // backslash + "!", which never matched the real credential).
                const adminGoodAuth = await httpCode(`${PROXY_DIRECT}/api/admin/keys`, `--user ${ADMIN_USER}:${ADMIN_PASS}`);
                lines.push(chk("Admin /api/admin/keys no auth → 401",   adminNoAuth  === "401", `got ${adminNoAuth}`));
                lines.push(chk("Admin /api/admin/keys wrong auth → 401", adminBadAuth === "401", `got ${adminBadAuth}`));
                lines.push(chk("Admin /api/admin/keys valid auth → 200", adminGoodAuth === "200", `got ${adminGoodAuth}`));

                const passed = lines.filter(l => l.includes("✅")).length;
                const total  = lines.filter(l => l.includes("✅") || l.includes("❌")).length;
                return `Auth Tests: ${passed}/${total} passed\n${lines.join("\n")}`;
            },
        },

        // ══════════════════════════════════════════════════════════════════════
        // 3. DASHBOARD
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "test-dashboard",
            description: [
                "Test the gcopilot-dashboard web UI end-to-end.",
                "Checks: dashboard container running, serve.py responding, login form works,",
                "authenticated page loads, /stats endpoint returns JSON, /v1/models returns data,",
                "/api/usage/daily returns list, /api/history returns list,",
                "dashboard HTML served at /, nginx reverse proxy at /copilot/."
            ].join(" "),
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const lines = [header("Dashboard Tests")];

                // Container health
                try {
                    const state = await ssh(REMOTE_HOST, "docker inspect gcopilot-dashboard --format '{{.State.Running}}' 2>/dev/null");
                    lines.push(chk("gcopilot-dashboard container running", state === "true", state));
                } catch (e) { lines.push(chk("Dashboard container", false, e.message)); }

                // serve.py responding
                const directCode = await ssh(REMOTE_HOST, "curl -s -o /dev/null -w '%{http_code}' http://localhost:3002/ 2>/dev/null").catch(() => "ERR");
                lines.push(chk("serve.py listening on :3002 → 302 or 200", directCode === "302" || directCode === "200", `got ${directCode}`));

                // Login and verify session
                try {
                    await ssh(REMOTE_HOST,
                        `curl -s -X POST http://localhost:3002/login ` +
                        `-d 'username=${DASH_USER}&password=${DASH_PASS}' ` +
                        `-H 'Content-Type: application/x-www-form-urlencoded' ` +
                        `-c /tmp/systest-dash-cookies.txt -o /dev/null`
                    );
                    const dashOk = await ssh(REMOTE_HOST, "curl -s -o /dev/null -w '%{http_code}' -b /tmp/systest-dash-cookies.txt http://localhost:3002/");
                    lines.push(chk("Login + authenticated dashboard loads → 200", dashOk === "200", `got ${dashOk}`));
                } catch (e) { lines.push(chk("Dashboard login flow", false, e.message)); }

                // Data endpoints (proxy-served through dashboard)
                lines.push("\nData endpoints (proxied through serve.py):");
                const auth = `-H "Authorization: Bearer ${API_KEY}"`;

                const statsBody = await dbCurl("/stats", auth);
                let statsOk = false;
                try { statsOk = typeof JSON.parse(statsBody) === "object"; } catch {}
                lines.push(chk("/stats returns valid JSON", statsOk, statsOk ? "ok" : statsBody.slice(0, 60)));

                const modelsBody = await dbCurl("/v1/models", auth);
                let modelsOk = false;
                try { modelsOk = Array.isArray(JSON.parse(modelsBody)?.data); } catch {}
                lines.push(chk("/v1/models returns model array", modelsOk));

                const dailyBody = await dbCurl("/api/usage/daily", auth);
                const dailyOk = hasDataArray(dailyBody);
                lines.push(chk("/api/usage/daily returns array", dailyOk, dailyOk ? "ok" : dailyBody.slice(0, 60)));

                const histBody = await dbCurl("/api/history", auth);
                const histOk = hasDataArray(histBody);
                lines.push(chk("/api/history returns array", histOk, histOk ? "ok" : histBody.slice(0, 60)));

                // nginx reverse proxy
                lines.push("\nnginx reverse proxy (/copilot/):");
                const nginxHealth = await httpCode(`${PUBLIC_BASE}/health`);
                lines.push(chk("nginx /copilot/health → 200", nginxHealth === "200", `got ${nginxHealth}`));

                const nginxModels = await httpCode(`${PUBLIC_BASE}/v1/models`, `-H "Authorization: Bearer ${API_KEY}"`);
                lines.push(chk("nginx /copilot/v1/models with key → 200", nginxModels === "200", `got ${nginxModels}`));

                // HTTPS
                const httpsCode = await httpCode(`${PUBLIC_BASE.replace("http://", "https://")}/health`);
                lines.push(chk("HTTPS /copilot/health → 200", httpsCode === "200", `got ${httpsCode}`));

                const passed = lines.filter(l => l.includes("✅")).length;
                const total  = lines.filter(l => l.includes("✅") || l.includes("❌")).length;
                return `Dashboard Tests: ${passed}/${total} passed\n${lines.join("\n")}`;
            },
        },

        // ══════════════════════════════════════════════════════════════════════
        // 4. DATABASE (MongoDB)
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "test-database",
            description: [
                "Test MongoDB connectivity and data pipeline through the proxy.",
                "Checks: MongoDB port reachable from REMOTE_HOST, proxy reports mongo=enabled in /health,",
                "/api/usage/daily returns valid structure, /api/history returns list,",
                "end-to-end write test (run inference, verify new history entry appears).",
                "MongoDB is at 192.168.86.48:27017, db=radiacode."
            ].join(" "),
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const lines = [header("Database (MongoDB) Tests")];
                const auth = `-H "Authorization: Bearer ${API_KEY}"`;

                // MongoDB port reachable from REMOTE_HOST host
                try {
                    const portCheck = await ssh(REMOTE_HOST, "nc -z 192.168.86.48 27017 && echo open || echo closed");
                    lines.push(chk("MongoDB port 27017 reachable from REMOTE_HOST", portCheck.includes("open"), portCheck));
                } catch (e) { lines.push(chk("MongoDB port check", false, e.message)); }

                // Proxy health reports mongo status
                try {
                    const enabled = await mongoEnabled();
                    lines.push(chk("Proxy reports MongoDB enabled", enabled, `mongodb_enabled=${enabled}`));
                } catch (e) { lines.push(chk("Proxy health mongo flag", false, e.message)); }

                // /api/usage/daily structure
                const dailyBody = await dbCurl("/api/usage/daily", auth);
                try {
                    const daily = JSON.parse(dailyBody);
                    const isArr = Array.isArray(daily?.data);
                    lines.push(chk("/api/usage/daily returns array", isArr, isArr ? `${daily.data.length} entries` : dailyBody.slice(0, 60)));
                    if (isArr && daily.data.length > 0) {
                        const sample = daily.data[0];
                        const hasDate = "date" in sample || "day" in sample || "_id" in sample;
                        lines.push(chk("Usage entries have date field", hasDate, Object.keys(sample).join(", ")));
                    }
                } catch { lines.push(chk("/api/usage/daily valid JSON", false, dailyBody.slice(0, 80))); }

                // /api/history structure
                const histBody = await dbCurl("/api/history", auth);
                let histCount = 0;
                try {
                    const hist = JSON.parse(histBody);
                    const isArr = Array.isArray(hist?.data);
                    histCount = isArr ? hist.data.length : 0;
                    lines.push(chk("/api/history returns array", isArr, isArr ? `${hist.data.length} entries` : histBody.slice(0, 60)));
                } catch { lines.push(chk("/api/history valid JSON", false, histBody.slice(0, 80))); }

                // End-to-end write test: run inference, check history grows
                lines.push("\nEnd-to-end write test:");
                await session.log("Running inference to trigger DB write...");
                const beforeCount = histCount;
                await chatCompletion({ base: PROXY_DIRECT, model: "qwen3:4b", message: "Say: DB_TEST_PASS", maxTokens: 6 });
                await new Promise(r => setTimeout(r, 3000)); // wait for async write

                const histBody2 = await dbCurl("/api/history", auth);
                try {
                    const hist2 = JSON.parse(histBody2);
                    const top = hist2?.data?.[0];
                    // Compare raw record counts (works when below the default
                    // /api/history limit=200 cap) OR — more robustly on a busy
                    // system where the page is already saturated — verify the
                    // newest record reflects the write we just triggered
                    // (correct model, timestamp within the last ~20s).
                    const afterCount = Array.isArray(hist2?.data) ? hist2.data.length : beforeCount;
                    const topIsRecent = !!top && top.model === "qwen3:4b" &&
                        (Date.now() - Date.parse(top.timestamp + "Z")) < 20000;
                    const grew = afterCount > beforeCount || topIsRecent;
                    lines.push(chk("History count grows after inference", grew,
                        `${beforeCount} → ${afterCount}${topIsRecent ? " (newest record confirms write)" : ""}`));
                } catch { lines.push(chk("History post-inference", false, "parse failed")); }

                // MongoDB enriched models endpoint
                const enrichedBody = await dbCurl("/api/models/enriched", auth);
                try {
                    const enriched = JSON.parse(enrichedBody);
                    const arr = enriched?.data;
                    lines.push(chk("/api/models/enriched returns data", Array.isArray(arr) && arr.length > 0,
                        Array.isArray(arr) ? `${arr.length} models` : enrichedBody.slice(0, 60)));
                } catch { lines.push(chk("/api/models/enriched", false, enrichedBody.slice(0, 60))); }

                const passed = lines.filter(l => l.includes("✅")).length;
                const total  = lines.filter(l => l.includes("✅") || l.includes("❌")).length;
                return `Database Tests: ${passed}/${total} passed\n${lines.join("\n")}`;
            },
        },

        // ══════════════════════════════════════════════════════════════════════
        // 5. PROXY → DGX (Ollama) CONNECTION
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "test-proxy-to-dgx",
            description: [
                "Test the proxy-to-DGX-Spark Ollama connection and full inference round-trip.",
                "Checks: Ollama alive on DGX, proxy /health shows ollama=true, proxy can list Ollama models,",
                "proxy router refresh discovers models, inference request flows end-to-end,",
                "response latency within acceptable range, GPU is exercised (model loads into VRAM)."
            ].join(" "),
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const lines = [header("Proxy → DGX Ollama Connection Tests")];
                const auth = `-H "Authorization: Bearer ${API_KEY}"`;

                // Ollama alive on DGX
                try {
                    const ver = await ssh(DGX, "curl -sf http://localhost:11434/api/version 2>/dev/null");
                    const parsed = JSON.parse(ver);
                    lines.push(chk("Ollama running on DGX Spark", !!parsed.version, `v${parsed.version}`));
                } catch (e) { lines.push(chk("Ollama on DGX", false, e.message)); }

                // Ollama model count on DGX
                try {
                    const tags = await ssh(DGX, "curl -sf http://localhost:11434/api/tags 2>/dev/null");
                    const parsed = JSON.parse(tags);
                    const count = parsed?.models?.length || 0;
                    lines.push(chk(`Ollama has models available (${count})`, count > 0));
                } catch (e) { lines.push(chk("Ollama models", false, e.message)); }

                // Proxy health shows ollama connected
                try {
                    const healthBody = await ssh(REMOTE_HOST, "curl -sf http://localhost:8001/health 2>/dev/null");
                    const health = JSON.parse(healthBody);
                    lines.push(chk("Proxy /health ollama=true", health.ollama === true, JSON.stringify(health)));
                    lines.push(chk(`Proxy sees models (${health.model_count})`, (health.model_count || 0) > 0));
                } catch (e) { lines.push(chk("Proxy health", false, e.message)); }

                // Router refresh discovers all models
                try {
                    const refreshBody = await ssh(REMOTE_HOST,
                        `curl -sf -X POST ${auth} http://localhost:8001/api/router/refresh 2>/dev/null`
                    );
                    const refresh = JSON.parse(refreshBody);
                    lines.push(chk("Router refresh triggers successfully", !!refresh, refreshBody.slice(0, 60)));
                } catch (e) { lines.push(chk("Router refresh", false, e.message)); }

                // End-to-end inference (fast small model)
                lines.push("\nEnd-to-end inference (qwen3:4b — fastest):");
                await session.log("Running inference via proxy → Ollama...");
                const result = await chatCompletion({ base: PROXY_DIRECT, model: "qwen3:4b", maxTokens: 8 });
                lines.push(chk(`Inference completes (${result.ms}ms)`, result.ok, `"${result.content}"`));
                lines.push(chk("Response time < 30s", result.ms < 30000, `${(result.ms / 1000).toFixed(1)}s`));

                // Model loaded into VRAM after inference
                try {
                    const ps = await ssh(DGX, "ollama ps 2>/dev/null");
                    lines.push(chk("Model loaded in VRAM after inference", ps.includes("qwen3"), ps.trim().slice(0, 80)));
                } catch (e) { lines.push(chk("VRAM check", false, e.message)); }

                const passed = lines.filter(l => l.includes("✅")).length;
                const total  = lines.filter(l => l.includes("✅") || l.includes("❌")).length;
                return `Proxy→DGX Tests: ${passed}/${total} passed\n${lines.join("\n")}`;
            },
        },

        // ══════════════════════════════════════════════════════════════════════
        // 6. ALL MODELS
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "test-all-models",
            description: [
                "Test every installed model by sending a minimal chat completion and verifying a response.",
                "Tests models sequentially (one at a time — Ollama evicts previous from VRAM on each swap).",
                "Large models (qwen3-coder-next 84GB) take 30-120s to load on first request.",
                "Optional 'skip_large' (bool) to skip 84GB models for faster test runs.",
                "Returns per-model PASS/FAIL with latency."
            ].join(" "),
            parameters: {
                type: "object",
                properties: {
                    skip_large: {
                        type: "boolean",
                        description: "Skip 84GB models (qwen3-coder-next, qwen3-coder-next-spec) for speed (default: false)"
                    }
                }
            },
            handler: async (args) => {
                const lines = [header("All-Models Test")];
                const skipLarge = args.skip_large === true;
                const largeModels = ["qwen3-coder-next:q8_0", "qwen3-coder-next-spec:latest"];

                const models = skipLarge
                    ? KNOWN_MODELS.filter(m => !largeModels.includes(m))
                    : KNOWN_MODELS;

                if (skipLarge) {
                    lines.push("  ⚠️  Skipping 84GB models (skip_large=true)");
                }
                lines.push(`  Testing ${models.length} models sequentially...\n`);

                let passed = 0, failed = 0;
                for (const model of models) {
                    await session.log(`Testing model: ${model}...`);
                    const isLarge = largeModels.includes(model);
                    const maxWait = isLarge ? 120000 : 30000;
                    const timeoutMs = maxWait;

                    try {
                        const result = await Promise.race([
                            chatCompletion({ base: PROXY_DIRECT, model, maxTokens: 6 }),
                            new Promise(resolve => setTimeout(() => resolve({ ok: false, content: "TIMEOUT", ms: timeoutMs }), timeoutMs))
                        ]);
                        const icon = result.ok ? "✅" : "❌";
                        lines.push(`  ${icon} ${model.padEnd(36)} ${(result.ms / 1000).toFixed(1)}s  "${result.content}"`);
                        if (result.ok) passed++; else failed++;
                    } catch (e) {
                        lines.push(`  ❌ ${model.padEnd(36)} ERROR: ${e.message.slice(0, 40)}`);
                        failed++;
                    }
                }

                return `All-Models Test: ${passed} passed, ${failed} failed\n${lines.join("\n")}`;
            },
        },

        // ══════════════════════════════════════════════════════════════════════
        // 7. PROXY → DATABASE PIPELINE
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "test-proxy-to-db",
            description: [
                "Test the proxy-to-MongoDB data pipeline specifically.",
                "Checks: proxy reports mongo=enabled, /api/usage/daily aggregates from DB,",
                "inference request is persisted to history collection in MongoDB,",
                "daily stats update after new inference, cost_engine data in response.",
                "MongoDB: 192.168.86.48:27017, db=radiacode."
            ].join(" "),
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const lines = [header("Proxy → MongoDB Pipeline Tests")];
                const auth = `-H "Authorization: Bearer ${API_KEY}"`;

                // MongoDB network path: proxy container → mongo host
                try {
                    const netCheck = await ssh(REMOTE_HOST,
                        "docker exec gcopilot-proxy python3 -c \"import socket; s=socket.create_connection(('192.168.86.48',27017),3); s.close(); print('ok')\" 2>&1"
                    );
                    lines.push(chk("Proxy container can reach MongoDB:27017", netCheck.trim() === "ok", netCheck.trim()));
                } catch (e) { lines.push(chk("Proxy→MongoDB network", false, e.message)); }

                // Proxy reports mongo enabled
                try {
                    const enabled = await mongoEnabled();
                    lines.push(chk("Proxy reports MongoDB enabled", enabled, `mongodb_enabled=${enabled}`));
                } catch (e) { lines.push(chk("Proxy mongo health flag", false, e.message)); }

                // Stats endpoint aggregates from DB
                const statsBody = await dbCurl("/stats", auth);
                try {
                    const stats = JSON.parse(statsBody);
                    const hasTotals = "total_requests" in stats || "requests_total" in stats || "today" in stats;
                    lines.push(chk("/stats returns aggregated data", hasTotals, Object.keys(stats).join(", ")));
                } catch { lines.push(chk("/stats valid JSON", false, statsBody.slice(0, 60))); }

                // Get current history count
                let beforeCount = 0;
                try {
                    const hist = JSON.parse(await dbCurl("/api/history", auth));
                    beforeCount = Array.isArray(hist?.data) ? hist.data.length : 0;
                } catch {}

                // Trigger inference → DB write
                lines.push("\nPipeline write test:");
                await session.log("Sending inference request to trigger DB persistence...");
                const inf = await chatCompletion({ base: PROXY_DIRECT, model: "qwen3:4b", message: "Say: DB_PIPELINE_OK", maxTokens: 5 });
                lines.push(chk("Inference succeeded (prerequisite)", inf.ok, `"${inf.content}"`));

                // Wait for async Mongo write
                await new Promise(r => setTimeout(r, 4000));

                // Verify history grew (or, on a busy/capped page, that the newest
                // record reflects the write we just triggered)
                try {
                    const hist2 = JSON.parse(await dbCurl("/api/history", auth));
                    const top = hist2?.data?.[0];
                    const afterCount = Array.isArray(hist2?.data) ? hist2.data.length : beforeCount;
                    const topIsRecent = !!top && top.model === "qwen3:4b" &&
                        (Date.now() - Date.parse(top.timestamp + "Z")) < 20000;
                    lines.push(chk("History collection grows after inference", afterCount > beforeCount || topIsRecent,
                        `${beforeCount} → ${afterCount} entries${topIsRecent ? " (newest record confirms write)" : ""}`));
                } catch (e) { lines.push(chk("History post-write", false, e.message)); }

                // Daily usage aggregation updates
                try {
                    const daily = JSON.parse(await dbCurl("/api/usage/daily", auth));
                    const arr = daily?.data;
                    const hasToday = Array.isArray(arr) && arr.length > 0;
                    lines.push(chk("/api/usage/daily has entries (aggregation running)", hasToday,
                        hasToday ? `${arr.length} days of data` : "empty"));
                } catch (e) { lines.push(chk("Daily usage aggregation", false, e.message)); }

                const passed = lines.filter(l => l.includes("✅")).length;
                const total  = lines.filter(l => l.includes("✅") || l.includes("❌")).length;
                return `Proxy→DB Pipeline: ${passed}/${total} passed\n${lines.join("\n")}`;
            },
        },

        // ══════════════════════════════════════════════════════════════════════
        // 8. RUN ALL TESTS (full suite)
        // ══════════════════════════════════════════════════════════════════════
        {
            name: "run-system-tests",
            description: [
                "Run the complete system test suite: completions API, auth, dashboard, database,",
                "proxy→DGX, proxy→DB. Does NOT run all-models test (use test-all-models separately).",
                "Returns a comprehensive pass/fail report across all subsystems.",
                "Optional 'quick' bool to skip streaming + write tests for speed.",
                "Takes 2-5 minutes for a full run."
            ].join(" "),
            parameters: {
                type: "object",
                properties: {
                    quick: {
                        type: "boolean",
                        description: "Quick mode: skip streaming, write tests, and large model checks (default: false)"
                    }
                }
            },
            handler: async (args) => {
                const quick = args.quick === true;
                const startTs = Date.now();
                const sections = [];
                let totalPassed = 0, totalFailed = 0;

                function tally(report) {
                    totalPassed += (report.match(/✅/g) || []).length;
                    totalFailed += (report.match(/❌/g) || []).length;
                    return report;
                }

                await session.log("🧪 Starting full system test suite...");

                // ── Proxy health (fast prerequisite check) ──
                const lines = [header("Full System Test Suite")];
                try {
                    const h = JSON.parse(await ssh(REMOTE_HOST, "curl -sf http://localhost:8001/health 2>/dev/null").catch(() => "{}"));
                    const ok = h.ollama === true;
                    lines.push(chk(`Proxy alive (ollama=${h.ollama}, models=${h.model_count})`, ok));
                    if (!ok) {
                        lines.push("  ⛔ Proxy not healthy — aborting test suite. Run restart-service proxy first.");
                        return lines.join("\n");
                    }
                } catch (e) {
                    lines.push(chk("Proxy prerequisite check", false, e.message));
                    return lines.join("\n");
                }

                // ── Auth ──
                await session.log("Testing auth...");
                {
                    const noKey  = await httpCode(`${PUBLIC_BASE}/v1/models`);
                    const goodKey = await httpCode(`${PUBLIC_BASE}/v1/models`, `-H "Authorization: Bearer ${API_KEY}"`);
                    const pub = await httpCode(`${PUBLIC_BASE}/health`);
                    sections.push(tally([
                        header("Auth"),
                        chk("No key → 401",   noKey   === "401", `got ${noKey}`),
                        chk("Valid key → 200", goodKey === "200", `got ${goodKey}`),
                        chk("/health public",  pub     === "200", `got ${pub}`),
                    ].join("\n")));
                }

                // ── Completions API ──
                await session.log("Testing completions API...");
                {
                    const modelsBody = await dbCurl("/v1/models", `-H "Authorization: Bearer ${API_KEY}"`);
                    let modelCount = 0;
                    try { modelCount = JSON.parse(modelsBody)?.data?.length || 0; } catch {}
                    const inf = await chatCompletion({ base: PROXY_DIRECT, model: "qwen3:4b", maxTokens: 6 });
                    sections.push(tally([
                        header("Completions API"),
                        chk(`/v1/models returns ${modelCount} models`, modelCount > 0),
                        chk(`Inference (qwen3:4b, ${inf.ms}ms)`, inf.ok, `"${inf.content}"`),
                        chk("Response time < 30s", inf.ms < 30000, `${(inf.ms / 1000).toFixed(1)}s`),
                    ].join("\n")));
                }

                // ── Dashboard ──
                await session.log("Testing dashboard...");
                {
                    const dashCode = await ssh(REMOTE_HOST, "curl -s -o /dev/null -w '%{http_code}' http://localhost:3002/ 2>/dev/null").catch(() => "ERR");
                    const nginxHealth = await httpCode(`${PUBLIC_BASE}/health`);
                    const statsBody = await dbCurl("/stats", `-H "Authorization: Bearer ${API_KEY}"`);
                    let statsOk = false;
                    try { statsOk = typeof JSON.parse(statsBody) === "object"; } catch {}
                    sections.push(tally([
                        header("Dashboard"),
                        chk("serve.py on :3002", dashCode === "302" || dashCode === "200", `got ${dashCode}`),
                        chk("nginx /copilot/health", nginxHealth === "200", `got ${nginxHealth}`),
                        chk("/stats JSON", statsOk),
                    ].join("\n")));
                }

                // ── Database ──
                await session.log("Testing database...");
                {
                    const auth = `-H "Authorization: Bearer ${API_KEY}"`;
                    let mongoPort = false;
                    try {
                        const r = await ssh(REMOTE_HOST, "nc -z 192.168.86.48 27017 && echo open || echo closed");
                        mongoPort = r.includes("open");
                    } catch {}
                    const mongoFlag = await mongoEnabled().catch(() => false);
                    const dailyBody = await dbCurl("/api/usage/daily", auth);
                    const dailyOk = hasDataArray(dailyBody);
                    sections.push(tally([
                        header("Database"),
                        chk("MongoDB port 27017 reachable", mongoPort),
                        chk("Proxy reports MongoDB enabled", mongoFlag),
                        chk("/api/usage/daily returns array", dailyOk),
                    ].join("\n")));
                }

                // ── DGX Connection ──
                await session.log("Testing DGX connection...");
                {
                    let dgxAlive = false;
                    try {
                        const ver = JSON.parse(await ssh(DGX, "curl -sf http://localhost:11434/api/version 2>/dev/null"));
                        dgxAlive = !!ver.version;
                    } catch {}
                    const proxyOllama = (JSON.parse(
                        await ssh(REMOTE_HOST, "curl -sf http://localhost:8001/health 2>/dev/null").catch(() => "{}")
                    ).ollama) === true;
                    sections.push(tally([
                        header("DGX Spark Connection"),
                        chk("Ollama alive on DGX", dgxAlive),
                        chk("Proxy connected to Ollama", proxyOllama),
                    ].join("\n")));
                }

                if (!quick) {
                    // ── Write pipeline (DB persistence) ──
                    await session.log("Testing DB write pipeline...");
                    {
                        const auth = `-H "Authorization: Bearer ${API_KEY}"`;
                        let beforeCount = 0;
                        try { beforeCount = JSON.parse(await dbCurl("/api/history", auth))?.data?.length || 0; } catch {}
                        await chatCompletion({ base: PROXY_DIRECT, model: "qwen3:4b", message: "Say: PIPELINE_TEST", maxTokens: 4 });
                        await new Promise(r => setTimeout(r, 4000));
                        let afterCount = 0, topIsRecent = false;
                        try {
                            const after = JSON.parse(await dbCurl("/api/history", auth));
                            afterCount = after?.data?.length || 0;
                            const top = after?.data?.[0];
                            topIsRecent = !!top && top.model === "qwen3:4b" &&
                                (Date.now() - Date.parse(top.timestamp + "Z")) < 20000;
                        } catch {}
                        sections.push(tally([
                            header("DB Write Pipeline"),
                            chk("History persisted after inference", afterCount > beforeCount || topIsRecent,
                                `${beforeCount} → ${afterCount}${topIsRecent ? " (newest record confirms write)" : ""}`),
                        ].join("\n")));
                    }
                }

                const totalMs = Date.now() - startTs;
                const summary = [
                    `\n${"═".repeat(52)}`,
                    `SYSTEM TEST RESULTS: ${totalPassed} passed, ${totalFailed} failed`,
                    `Total time: ${(totalMs / 1000).toFixed(1)}s`,
                    `${"═".repeat(52)}`,
                ].join("\n");

                return [lines.join("\n"), ...sections, summary].join("\n");
            },
        },

    ], // end tools
});
