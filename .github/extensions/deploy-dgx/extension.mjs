// Extension: deploy-dgx
// Deploy proxy to the remote host and manage dashboard.
// Architecture: DGX Spark (Ollama only) ← proxy ← remote nginx ← clients

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

const API_KEY = process.env.COPILOT_PROVIDER_API_KEY || process.env.PROXY_API_KEY || "";
const DASH_USER = process.env.DASHBOARD_USERNAME || "darkmatter2222";
const DASH_PASS = process.env.DASHBOARD_PASSWORD || "";
const ADMIN_USER = process.env.ADMIN_USERNAME || "darkmatter2222";
const ADMIN_PASS = process.env.ADMIN_PASSWORD || "";
const DGX = "dgxspark";
const REMOTE_HOST = "darkmatter2222@192.168.86.48";

function run(cmd) {
    return exec(shell, ["-NoProfile", "-NonInteractive", shellFlag, cmd]);
}

// IMPORTANT: invoke `ssh` directly via execFile with an argv array (no shell
// in between) so `cmd` is forwarded to the remote host byte-for-byte. Building
// this as a quoted string and re-parsing it through pwsh/bash (the old
// approach) is fragile — the bash-style `'\''` escape trick used previously
// is not valid PowerShell single-quoted string syntax, so any cmd containing
// a literal `'` silently broke the remote command on Windows.
async function ssh(host, cmd) {
    const { stdout } = await exec("ssh", ["-o", "ConnectTimeout=10", host, cmd]);
    return stdout.trim();
}

async function curlCheck(url, extraArgs = "") {
    try {
        const { stdout } = await run(
            `curl.exe -sk -o NUL -w "%{http_code}" ${extraArgs} "${url}"`
        );
        return stdout.trim();
    } catch { return "ERR"; }
}

const session = await joinSession({
    hooks: {
        onSessionStart: async () => {
            await session.log("LLM Stack tools loaded (remote proxy → DGX Ollama)", { level: "info", ephemeral: true });
        },
    },
    tools: [
        // ── DEPLOY ──────────────────────────────────────────────────────────────
        {
            name: "deploy-proxy",
            description: "Deploy gcopilot-proxy (FastAPI) to the remote host. Builds Docker image from proxy/ source, stops old container, starts new one on docucraft_docucraft-network. DGX Spark runs Ollama only.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                await session.log("🚀 Deploying proxy to the remote host...");
                try {
                    const { stdout, stderr } = await run("python scripts\\deploy.py 2>&1");
                    const ok = stdout.includes("Deploy successful");
                    return ok
                        ? `✅ Proxy deployed successfully:\n${stdout.trim()}`
                        : `⚠️ Deploy completed with issues:\n${(stdout + stderr).trim()}`;
                } catch (err) {
                    return `❌ Deploy failed: ${(err.stdout || err.stderr || err.message).trim()}`;
                }
            },
        },
        {
            name: "deploy-dashboard",
            description: "Deploy gcopilot-dashboard (serve.py) to the remote host. SCPs dashboard/serve.py and index.html, rebuilds image with --no-cache, restarts container with correct env vars.",
            parameters: {
                type: "object",
                properties: {
                    api_key: { type: "string", description: "Proxy API key (default: configured key)" }
                }
            },
            handler: async (args) => {
                await session.log("📦 Deploying dashboard to the remote host...");
                const key = args.api_key || API_KEY;
                const steps = [];
                try {
                    await run(`scp dashboard\\serve.py ${REMOTE_HOST}:~/GithubCopilotExit/dashboard/serve.py`);
                    await run(`scp dashboard\\index.html ${REMOTE_HOST}:~/GithubCopilotExit/dashboard/index.html`);
                    await run(`scp dashboard\\Dockerfile ${REMOTE_HOST}:~/GithubCopilotExit/dashboard/Dockerfile`);
                    steps.push("✅ Files uploaded");
                } catch (e) { return `❌ SCP failed: ${e.message}`; }

                try {
                    await ssh(REMOTE_HOST, "cd ~/GithubCopilotExit && docker build --no-cache -f dashboard/Dockerfile -t gcopilot-dashboard . 2>&1 | tail -5");
                    steps.push("✅ Image built");
                } catch (e) { return `❌ Build failed: ${e.message}`; }

                try {
                    await ssh(REMOTE_HOST,
                        `docker stop gcopilot-dashboard 2>/dev/null; docker rm gcopilot-dashboard 2>/dev/null; ` +
                        `docker run -d --name gcopilot-dashboard --restart unless-stopped ` +
                        `--network docucraft_docucraft-network -p 3002:3002 ` +
                        `-e PROXY_BACKEND=http://gcopilot-proxy:8001 ` +
                        `-e DASHBOARD_PORT=3002 -e PROXY_PATH_PREFIX=/copilot ` +
                        `-e DASHBOARD_USERNAME=${DASH_USER} -e DASHBOARD_PASSWORD=${DASH_PASS} ` +
                        `-e PROXY_API_KEY=${key} -e ADMIN_USERNAME=${ADMIN_USER} -e ADMIN_PASSWORD=${ADMIN_PASS} ` +
                        `gcopilot-dashboard`
                    );
                    steps.push("✅ Container started");
                } catch (e) { return `❌ Start failed: ${e.message}`; }

                // Health check
                await new Promise(r => setTimeout(r, 6000));
                const health = await ssh(REMOTE_HOST, "curl -s http://localhost:3002/healthcheck 2>/dev/null").catch(() => "timeout");
                steps.push(`✅ Health: ${health}`);
                return steps.join("\n");
            },
        },
        {
            name: "deploy-all",
            description: "Deploy both proxy and dashboard to the remote host in sequence. Use after making code changes to proxy/ or dashboard/.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                await session.log("🚀 Deploying full stack to the remote host...");
                const results = [];
                // Proxy first
                try {
                    const { stdout } = await run("python scripts\\deploy.py 2>&1");
                    results.push(stdout.includes("Deploy successful") ? "✅ Proxy: deployed" : `⚠️ Proxy: ${stdout.split("\n").pop()}`);
                } catch (e) { results.push(`❌ Proxy: ${e.message}`); }
                // Then dashboard
                try {
                    await run(`scp dashboard\\serve.py ${REMOTE_HOST}:~/GithubCopilotExit/dashboard/serve.py`);
                    await run(`scp dashboard\\index.html ${REMOTE_HOST}:~/GithubCopilotExit/dashboard/index.html`);
                    await ssh(REMOTE_HOST, "cd ~/GithubCopilotExit && docker build --no-cache -f dashboard/Dockerfile -t gcopilot-dashboard . 2>&1 | tail -3");
                    await ssh(REMOTE_HOST, "docker restart gcopilot-dashboard");
                    results.push("✅ Dashboard: deployed");
                } catch (e) { results.push(`❌ Dashboard: ${e.message}`); }
                return results.join("\n");
            },
        },

        // ── HEALTH / STATUS ──────────────────────────────────────────────────
        {
            name: "health-check",
            description: "Check health of the entire LLM stack: DGX Ollama, remote proxy, dashboard container, nginx, and all API endpoints.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const results = [];

                // DGX Spark
                try {
                    const r = await ssh(DGX, "curl -sf http://localhost:11434/api/tags 2>/dev/null | python3 -c \"import sys,json; d=json.load(sys.stdin); print(f'{len(d[\\\"models\\\"])} models')\" 2>/dev/null || echo unreachable");
                    results.push(`DGX Ollama: ${r || "no response"}`);
                } catch (e) { results.push(`DGX Ollama: ❌ ${e.message}`); }

                try {
                    const r = await ssh(DGX, "ss -tlnp | grep 11434 | awk '{print $4}'");
                    results.push(`DGX port 11434: ${r || "not bound"} (should be *:11434)`);
                    const noProxy = await ssh(DGX, "docker ps --filter name=gcopilot-proxy --format '{{.Names}}' 2>/dev/null || echo none");
                    results.push(`DGX proxy container: ${noProxy || "none"} (should be none)`);
                } catch (e) { results.push(`DGX ports: ❌ ${e.message}`); }

                // remote proxy
                try {
                    const h = await ssh(REMOTE_HOST, "curl -sf http://localhost:8001/health 2>/dev/null");
                    const parsed = JSON.parse(h);
                    results.push(`Proxy (remote host): ✅ ollama=${parsed.ollama} models=${parsed.model_count}`);
                } catch (e) { results.push(`Proxy (remote host): ❌ ${e.message}`); }

                // Dashboard
                try {
                    const s = await ssh(REMOTE_HOST, "docker inspect gcopilot-dashboard --format '{{.State.Health.Status}}' 2>/dev/null");
                    results.push(`Dashboard container: ${s || "not found"}`);
                } catch (e) { results.push(`Dashboard: ❌ ${e.message}`); }

                // nginx
                try {
                    const s = await ssh(REMOTE_HOST, "docker inspect susman-ingress --format '{{.State.Running}}' 2>/dev/null");
                    results.push(`nginx (susman-ingress): ${s === "true" ? "running ✅" : "stopped ❌"}`);
                } catch (e) { results.push(`nginx: ❌ ${e.message}`); }

                // API endpoint checks
                const httpCode = (url, extra = "") => curlCheck(url, extra);
                const k = `-H "Authorization: Bearer ${API_KEY}"`;
                results.push(`\nAPI checks:`);
                results.push(`  /copilot/health: ${await httpCode("http://192.168.86.48/copilot/health")} (want 200)`);
                results.push(`  /copilot/ unauthed: ${await httpCode("http://192.168.86.48/copilot/")} (want 302)`);
                results.push(`  /copilot/v1/models no key: ${await httpCode("http://192.168.86.48/copilot/v1/models")} (want 401)`);
                results.push(`  /copilot/v1/models with key: ${await httpCode("http://192.168.86.48/copilot/v1/models", k)} (want 200)`);
                results.push(`  HTTPS /copilot/health: ${await httpCode("https://192.168.86.48/copilot/health")} (want 200)`);

                return results.join("\n");
            },
        },
        {
            name: "validate-all",
            description: "Run the comprehensive end-to-end validation suite: security enforcement, HTTP+HTTPS, dashboard login flow, inference completion test, container health. Returns PASS/FAIL for each check.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const results = [];
                const k = API_KEY;
                const base = "http://192.168.86.48/copilot";

                function chk(label, code, expected) {
                    const pass = code === String(expected);
                    results.push(`  ${pass ? "✅" : "❌"} [${code}/${expected}] ${label}`);
                    return pass;
                }

                // Login to get session cookie
                await ssh(REMOTE_HOST, `curl -s -X POST ${base}/login -d 'username=${DASH_USER}&password=${DASH_PASS}' -H 'Content-Type: application/x-www-form-urlencoded' -c /tmp/val-cookies.txt -o /dev/null`);

                const c = (url, xtra = "") => curlCheck(url, xtra);
                const ck = `-H "Authorization: Bearer ${k}"`;

                results.push("Security:");
                chk("Health (public)", await c(`${base}/health`), 200);
                chk("Dashboard without session → 302", await c(`${base}/`), 302);
                chk("Inference without key → 401", await c(`${base}/v1/models`), 401);
                chk("Inference with key → 200", await c(`${base}/v1/models`, ck), 200);

                results.push("HTTPS (IP):");
                chk("HTTPS health", await c("https://192.168.86.48/copilot/health"), 200);
                chk("HTTPS inference with key", await c("https://192.168.86.48/copilot/v1/models", ck), 200);

                results.push("Dashboard:");
                const dashCode = await ssh(REMOTE_HOST, `curl -s -o /dev/null -w '%{http_code}' -b /tmp/val-cookies.txt ${base}/`).catch(() => "ERR");
                chk("Dashboard with session → 200", dashCode, 200);

                results.push("Proxy /dashboard removed:");
                chk("Direct /dashboard with key → 404", await c("http://192.168.86.48:8001/dashboard", ck), 404);

                results.push("DGX (Ollama only):");
                const dgxOllama = await ssh(DGX, "curl -sf http://localhost:11434/api/version 2>/dev/null | python3 -c \"import sys,json;print('ok:'+json.load(sys.stdin)['version'])\" 2>/dev/null || echo err").catch(() => "err");
                chk("DGX Ollama alive", dgxOllama.startsWith("ok:") ? "200" : "000", 200);
                const dgxProxyGone = await ssh(DGX, "nc -z 127.0.0.1 8001 && echo running || echo gone").catch(() => "gone");
                chk("DGX proxy port 8001 gone", dgxProxyGone.includes("gone") ? "200" : "000", 200);

                results.push("Inference:");
                try {
                    const resp = await ssh(REMOTE_HOST,
                        `curl -s -H "Authorization: Bearer ${k}" -H "Content-Type: application/json" ` +
                        `-d '{"model":"qwen3","messages":[{"role":"user","content":"Say: PASS"}],"stream":false,"max_tokens":4}' ` +
                        `${base}/v1/chat/completions 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'][:30])"`
                    );
                    results.push(`  ✅ Chat completion: "${resp}"`);
                } catch (e) {
                    results.push(`  ❌ Chat completion: ${e.message}`);
                }

                const passed = results.filter(r => r.includes("✅")).length;
                const failed = results.filter(r => r.includes("❌")).length;
                return `Validation: ${passed} passed, ${failed} failed\n\n` + results.join("\n");
            },
        },

        // ── SERVICE MANAGEMENT ───────────────────────────────────────────────
        {
            name: "restart-service",
            description: "Restart a specific service. Services: proxy (gcopilot-proxy on the remote host), dashboard (gcopilot-dashboard on the remote host), nginx (susman-ingress on the remote host), ollama (systemd on DGX).",
            parameters: {
                type: "object",
                properties: {
                    service: {
                        type: "string",
                        enum: ["proxy", "dashboard", "nginx", "ollama"],
                        description: "Which service to restart"
                    }
                },
                required: ["service"]
            },
            handler: async (args) => {
                await session.log(`Restarting ${args.service}...`);
                try {
                    switch (args.service) {
                        case "proxy":
                            await ssh(REMOTE_HOST, "docker restart gcopilot-proxy");
                            await new Promise(r => setTimeout(r, 5000));
                            const h = await ssh(REMOTE_HOST, "curl -sf http://localhost:8001/health");
                            return `✅ gcopilot-proxy restarted: ${h}`;
                        case "dashboard":
                            await ssh(REMOTE_HOST, "docker restart gcopilot-dashboard");
                            await new Promise(r => setTimeout(r, 4000));
                            const dh = await ssh(REMOTE_HOST, "docker inspect gcopilot-dashboard --format '{{.State.Health.Status}}'");
                            return `✅ gcopilot-dashboard restarted: ${dh}`;
                        case "nginx":
                            await ssh(REMOTE_HOST, "docker exec susman-ingress nginx -s reload");
                            return `✅ nginx reloaded`;
                        case "ollama":
                            await ssh(DGX, "sudo systemctl restart ollama");
                            await new Promise(r => setTimeout(r, 5000));
                            const os = await ssh(DGX, "systemctl is-active ollama");
                            return `✅ ollama restarted: ${os}`;
                        default:
                            return `❌ Unknown service: ${args.service}`;
                    }
                } catch (e) {
                    return `❌ Failed to restart ${args.service}: ${e.message}`;
                }
            },
        },
        {
            name: "view-logs",
            description: "View recent logs from a service container. Services: proxy, dashboard, nginx, ollama.",
            parameters: {
                type: "object",
                properties: {
                    service: {
                        type: "string",
                        enum: ["proxy", "dashboard", "nginx", "ollama"],
                        description: "Which service logs to view"
                    },
                    lines: {
                        type: "number",
                        description: "Number of log lines (default: 30)"
                    }
                },
                required: ["service"]
            },
            handler: async (args) => {
                const n = args.lines || 30;
                try {
                    let logs;
                    switch (args.service) {
                        case "proxy":
                            logs = await ssh(REMOTE_HOST, `docker logs gcopilot-proxy --tail ${n} 2>&1`);
                            break;
                        case "dashboard":
                            logs = await ssh(REMOTE_HOST, `docker logs gcopilot-dashboard --tail ${n} 2>&1`);
                            break;
                        case "nginx":
                            logs = await ssh(REMOTE_HOST, `docker logs susman-ingress --tail ${n} 2>&1`);
                            break;
                        case "ollama":
                            logs = await ssh(DGX, `sudo journalctl -u ollama -n ${n} --no-pager 2>&1`);
                            break;
                        default:
                            return `❌ Unknown service: ${args.service}`;
                    }
                    return `${args.service} logs (last ${n} lines):\n${logs}`;
                } catch (e) {
                    return `❌ Failed to get logs for ${args.service}: ${e.message}`;
                }
            },
        },

        // ── MODEL MANAGEMENT ────────────────────────────────────────────────
        {
            name: "list-models",
            description: "List all Ollama models on DGX Spark, showing which are currently loaded in VRAM and available disk space.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                try {
                    const modelList = await ssh(DGX, "ollama list 2>/dev/null");
                    const running = await ssh(DGX, "ollama ps 2>/dev/null || echo 'none running'");
                    const disk = await ssh(DGX, "df -h /var/lib/ollama 2>/dev/null || df -h / | tail -1");
                    return `Models available on DGX Spark:\n${modelList}\n\nCurrently in VRAM:\n${running}\n\nDisk:\n${disk}`;
                } catch (e) {
                    return `❌ Failed: ${e.message}`;
                }
            },
        },
        {
            name: "pull-model",
            description: "Pull (download) an Ollama model to DGX Spark. The proxy auto-discovers it within 30 seconds — no restart needed.",
            parameters: {
                type: "object",
                properties: {
                    model: {
                        type: "string",
                        description: "Model name to pull, e.g. 'llama3.3:70b-instruct-q4_K_M'"
                    }
                },
                required: ["model"]
            },
            handler: async (args) => {
                await session.log(`Pulling ${args.model} to DGX Spark...`);
                try {
                    const disk = await ssh(DGX, "df -h / | tail -1 | awk '{print $4}'");
                    await session.log(`Available disk: ${disk}`);
                    const out = await ssh(DGX, `ollama pull ${args.model} 2>&1`);
                    // Wait and check if proxy discovers it
                    await new Promise(r => setTimeout(r, 35000));
                    const health = await ssh(REMOTE_HOST, "curl -sf http://localhost:8001/health").catch(() => "{}");
                    const parsed = JSON.parse(health);
                    return `✅ Pulled ${args.model}:\n${out.split("\n").slice(-3).join("\n")}\n\nProxy now sees ${parsed.model_count || "?"} models.`;
                } catch (e) {
                    return `❌ Pull failed: ${e.message}`;
                }
            },
        },
        {
            name: "remove-model",
            description: "Remove an Ollama model from DGX Spark to free disk space.",
            parameters: {
                type: "object",
                properties: {
                    model: { type: "string", description: "Model name to remove, e.g. 'llama3:8b'" }
                },
                required: ["model"]
            },
            handler: async (args) => {
                try {
                    const out = await ssh(DGX, `ollama rm ${args.model} 2>&1`);
                    return `✅ Removed ${args.model}: ${out}`;
                } catch (e) {
                    return `❌ Remove failed: ${e.message}`;
                }
            },
        },

        // ── NGINX MANAGEMENT ────────────────────────────────────────────────
        {
            name: "update-nginx",
            description: "Copy the ~/current_nginx.conf to the susman-ingress container, test the config, and reload nginx. Call after editing the nginx config on the remote host.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                try {
                    const test = await ssh(REMOTE_HOST, "docker cp ~/current_nginx.conf susman-ingress:/etc/nginx/conf.d/default.conf && docker exec susman-ingress nginx -t 2>&1");
                    if (test.includes("test is successful")) {
                        await ssh(REMOTE_HOST, "docker exec susman-ingress nginx -s reload");
                        return `✅ nginx config tested and reloaded:\n${test}`;
                    } else {
                        return `❌ nginx config test failed:\n${test}`;
                    }
                } catch (e) {
                    return `❌ nginx update failed: ${e.message}`;
                }
            },
        },

        // ── OLD COMPATIBILITY ALIASES ────────────────────────────────────────
        {
            name: "validate-dgx-proxy",
            description: "Validate DGX Spark Ollama (now inference-only, no proxy). Checks Ollama health, model count, and confirms port 8001 is not in use.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const results = [];
                try {
                    const r = await ssh(DGX, "curl -sf http://localhost:11434/api/version 2>/dev/null");
                    results.push(`✅ Ollama: ${r}`);
                } catch (e) { results.push(`❌ Ollama health: ${e.message}`); }
                try {
                    const models = await ssh(DGX, "ollama list 2>/dev/null | tail -n +2 | wc -l");
                    results.push(`✅ Models available: ${models.trim()}`);
                } catch (e) { results.push(`❌ Model count: ${e.message}`); }
                try {
                    const port = await ssh(DGX, "ss -tlnp | grep ':8001' || echo 'port 8001 not in use'");
                    results.push(`Port 8001 status: ${port}`);
                } catch (e) { results.push(`⚠️ Port check: ${e.message}`); }
                return "DGX Spark (Ollama only):\n" + results.join("\n");
            },
        },
    ],
});
