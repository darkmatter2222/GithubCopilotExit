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
                "Validate Databricks dashboard container. Checks 'Think' refs, grip icons, container status.",
            parameters: { type: "object", properties: {} },
            handler: async () => {
                let results = [];
                try {
                    const st = await ssh("databricks", `docker inspect gcopilot-dashboard --format '{{.State.Status}}' 2>/dev/null`);
                    results.push(`Container: ${st || "not found"}`);
                } catch (e) { results.push(`❌ Status: ${e.message}`); }
                try {
                    const tc = await ssh("databricks", `curl -s http://localhost:3002/ | grep -oi 'Think Tok' | wc -l`);
                    const totc = await ssh("databricks", `curl -s http://localhost:3002/ | grep -oi 'Total Tok' | wc -l`);
                    results.push(`Think count: ${tc} (expect 0)`);
                    results.push(`Total Tok count: ${totc} (expect >=1)`);
                } catch (e) { results.push(`❌ Content: ${e.message}`); }
                try {
                    const gc = await ssh("databricks", `curl -s http://localhost:3002/ | grep -c 'panel-grip'`);
                    results.push(`Gripe refs: ${gc} (expect >=7)`);
                } catch (e) { results.push(`❌ Gripp: ${e.message}`); }
                return "Databricks Validation:\n" + results.join("\n");
            },
        },
        {
            name: "sync-dashboard-databricks",
            description:
                "SCP dashboard/index.html and serve.py to Databricks, rebuild Docker image, restart container.",
            parameters: {
                type: "object",
                properties: { verify: { type: "boolean", description: "Run validation after (default true)" } },
            },
            handler: async (args) => {
                await session.log("📦 Syncing dashboard to Databricks...");
                let steps = [];
                try {
                    const R = "C:\\Users\\ryans\\source\\repos\\GithubCopilotExit";
                    await run(`scp "${R}\\dashboard\\index.html" databricks:~/GithubCopilotExit/dashboard/index.html`);
                    steps.push("✅ Copied index.html");
                } catch (e) { return `❌ SCP html: ${e.message}`; }
                try {
                    const R = "C:\\Users\\ryans\\source\\repos\\GithubCopilotExit";
                    await run(`scp "${R}\\dashboard\\serve.py" databricks:~/GithubCopilotExit/dashboard/serve.py`);
                    steps.push("✅ Copied serve.py");
                } catch (e) { return `❌ SCP py: ${e.message}`; }
                try {
                    await ssh("databricks", `cd ~/GithubCopilotExit && docker build -f dashboard/Dockerfile.deploy -t gcopilot-dashboard .`);
                    steps.push("✅ Rebuilt image");
                } catch (e) { return `❌ Build: ${e.message}`; }
                try {
                    await ssh("databricks", `docker stop gcopilot-dashboard 2>/dev/null; docker rm gcopilot-dashboard 2>/dev/null`);
                    await ssh("databricks", `docker run -d --name gcopilot-dashboard --restart unless-stopped -e PROXY_URL=http://192.168.86.39:8001 -e DASHBOARD_PORT=3002 --network host gcopilot-dashboard`);
                    steps.push("✅ Restarted container on :3002");
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
