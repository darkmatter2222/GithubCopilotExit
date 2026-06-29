// Extension: deploy-dgx
// Deploy and validate proxy and dashboard to DGX Spark and Databricks

import { joinSession } from "@github/copilot-sdk/extension";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const exec = promisify(execFile);
const isWin = globalThis.process?.platform === "win32";
const shell = isWin ? "pwsh" : "bash";
const shellArgs = isWin ? ["-NoProfile", "-NonInteractive"] : ["-c"];

function run(cmd) {
  const args = isWin ? [...shellArgs, cmd] : [...shellArgs, cmd];
  return exec(shell, args);
}

async function ssh(host, cmdStr) {
  const escaped = cmdStr.replace(/'/g, "'\\''");
  const cmd = `ssh ${host} '${escaped}'`;
  const { stdout } = await run(cmd);
  return stdout.trim();
}

const session = await joinSession({
    hooks: {
        onSessionStart: async () => {
            await session.log("deploy-dgx extension loaded", { level: "info", ephemeral: true });
        },
    },
    tools: [
        {
            name: "deploy-proxy-dgx",
            description:
                "Deploy the proxy to DGX Spark. Builds Docker image and restarts gcopilot-proxy container.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                await session.log("🚀 Deploying proxy to DGX Spark...");
                try {
                    const { stdout } = await run("python scripts/deploy.py");
                    return `✅ Proxy deployed:\n${stdout.trim()}`;
                } catch (err) {
                    return `❌ Deploy failed: ${(err.stdout || err.stderr || err.message).trim()}`;
                }
            },
        },
        {
            name: "validate-dgx-proxy",
            description:
                "Validate DGX Spark proxy health. Checks /health, /v1/models, /api/models/running.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const checks = [
                    ["Health", "Invoke-RestMethod -Uri http://192.168.86.39:8001/health | ConvertTo-Json -Depth 3"],
                    ["Models", "Invoke-RestMethod -Uri http://192.168.86.39:8001/v1/models | ConvertTo-Json -Compress"],
                    ["Running", "Invoke-RestMethod -Uri http://192.168.86.39:8001/api/models/running | ConvertTo-Json -Compress"],
                ];
                let results = [];
                for (const [name, cmd] of checks) {
                    try {
                        const { stdout } = await run(cmd);
                        results.push(`✅ ${name}: ${stdout.trim()}`);
                    } catch (err) {
                        results.push(`❌ ${name}: ${(err.stdout || err.stderr || err.message).trim()}`);
                    }
                }
                return "DGX Spark Validation:\n" + results.join("\n");
            },
        },
        {
            name: "validate-databricks-dashboard",
            description:
                "Validate Databricks dashboard container. Checks container status, serve.py path normalization, __BASE_PATH injection, grip icons, and all /copilot/ API endpoints via nginx.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                let results = [];
                // 1. Container status
                try {
                    const st = await ssh("databricks", `docker inspect gcopilot-dashboard --format '{{.State.Status}}' 2>/dev/null`);
                    results.push(`Container: ${st || "not found"}`);
                } catch (e) { results.push(`❌ Status: ${e.message}`); }
                // 2. serve.py path normalization
                try {
                    const np = await ssh("databricks", `docker exec gcopilot-dashboard grep '_norm_path' /srv/serve.py | head -1`);
                    results.push(`Path normalization: ${np ? 'present' : 'missing'}`);
                } catch (e) { results.push(`❌ NormPath: ${e.message}`); }
                // 3. __BASE_PATH injection in HTML
                try {
                    const bp = await ssh("databricks", `curl -s http://localhost:3002/ | grep '__BASE_PATH.*copilot'`);
                    results.push(`__BASE_PATH injected: ${bp ? 'yes' : 'no'}`);
                } catch (e) { results.push(`❌ BasePath: ${e.message}`); }
                // 4. All fetch calls use __bp
                try {
                    const fc = await ssh("databricks", `docker exec gcopilot-dashboard grep -c 'fetch(__bp' /srv/index.html`);
                    results.push(`fetch(__bp) calls: ${fc} (expect >=11)`);
                } catch (e) { results.push(`❌ FetchCount: ${e.message}`); }
                // 5. Grip icons
                try {
                    const gc = await ssh("databricks", `curl -s http://localhost:3002/ | grep -c 'panel-grip'`);
                    results.push(`Grip icon refs: ${gc} (expect >=7)`);
                } catch (e) { results.push(`❌ GripIcons: ${e.message}`); }
                // 6. Live data via nginx /copilot/ path
                try {
                    const stats = await ssh("databricks", `curl -sk https://127.0.0.1/copilot/stats -H "Host: susmannet.duckdns.org" | python3 -c "import sys,json;print(json.load(sys.stdin).get('success_count','FAIL'))"`);
                    results.push(`/copilot/stats: success_count=${stats}`);
                } catch (e) { results.push(`❌ NginxStats: ${e.message}`); }
                try {
                    const db = await ssh("databricks", `curl -sk https://127.0.0.1/copilot/api/stats/summary?days=1 -H "Host: susmannet.duckdns.org" | python3 -c "import sys,json;print(json.load(sys.stdin).get('total_requests','FAIL'))"`);
                    results.push(`/copilot/api/stats/summary: total_requests=${db}`);
                } catch (e) { results.push(`❌ NginxDB: ${e.message}`); }
                // 7. Container logs (last 5 lines, check for errors)
                try {
                    const logs = await ssh("databricks", `docker logs gcopilot-dashboard --tail 5`);
                    hasError = logs.includes('Traceback') || logs.includes('Error');
                    results.push(`Container logs: ${hasError ? 'HAS ERRORS' : 'clean'}`);
                } catch (e) { results.push(`⚠️ Logs: ${e.message}`); }
                return "Databricks Dashboard Validation:\n" + results.join("\n");
            },
        },
        {
            name: "sync-dashboard-databricks",
            description:
                "SCP dashboard/index.html and serve.py to Databrix, rebuild Docker image with --no-cache, stop old container, restart with correct env vars and network. CRITICAL: uses PROXY_BACKEND (not PROXY_URL), docucraft_docucraft-network, and PROXY_PATH_PREFIX=/copilot.",
            parameters: {
                type: "object",
                properties: { verify: { type: "boolean", description: "Run validation after (default true)" } },
            },
            handler: async (args) => {
                await session.log("📦 Syncing dashboard to Databricks...");
                let steps = [];
                const R = "C:\\Users\\ryans\\source\\repos\\GithubCopilotExit";
                // Step 1: SCP files
                try {
                    await run(`scp "${R}\\dashboard\\index.html" databricks:~/GithubCopilotExit/dashboard/index.html`);
                    steps.push("✅ Copied index.html");
                } catch (e) { return `❌ SCP html: ${e.message}`; }
                try {
                    await run(`scp "${R}\\dashboard\\serve.py" databricks:~/GithubCopilotExit/dashboard/serve.py`);
                    steps.push("✅ Copied serve.py");
                } catch (e) { return `❌ SCP py: ${e.message}`; }
                // Step 2: Rebuild image with --no-cache to avoid cached layers
                try {
                    await ssh("databricks", `cd ~/GithubCopilotExit && docker build --no-cache -f dashboard/Dockerfile.deploy -t gcopilot-dashboard .`);
                    steps.push("✅ Rebuilt image (no-cache)");
                } catch (e) { return `❌ Build: ${e.message}`; }
                // Step 3: Stop & remove old container
                try {
                    await ssh("databricks", `docker stop gcopilot-dashboard 2>/dev/null; docker rm gcopilot-dashboard 2>/dev/null`);
                    steps.push("✅ Removed old container");
                } catch (e) { return `❌ Remove: ${e.message}`; }
                // Step 4: Run new container with CORRECT env vars + network
                // Use PROXY_BACKEND (not PROXY_URL), docucraft_docucraft-network, PROXY_PATH_PREFIX=/copilot
                try {
                    await ssh("databricks", `docker run -d --name gcopilot-dashboard --restart unless-stopped --network docucraft_docucraft-network -p 3002:3002 -e PROXY_BACKEND=http://192.168.86.39:8001 -e DASHBOARD_PORT=3002 -e PROXY_PATH_PREFIX=/copilot gcopilot-dashboard`);
                    steps.push("✅ Started new container with correct config");
                } catch (e) { return `❌ Restart: ${e.message}`; }
                if (args.verify !== false) {
                    try {
                        await ssh("databricks", `sleep 3 && curl -s http://localhost:3002/ | head -c 100`);
                        steps.push("✅ Health OK");
                    } catch (e) { steps.push(`⚠️ Quick check: ${e.message}`); }
                }
                return "Sync complete:\n" + steps.join("\n");
            },
        },
    ],
});
