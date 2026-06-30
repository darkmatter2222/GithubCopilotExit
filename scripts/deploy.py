#!/usr/bin/env python3
"""
Deploy LLM Proxy to Databricks (192.168.86.48).

Architecture (new):
  - gcopilot-proxy runs on Databricks, on docucraft_docucraft-network
  - nginx upstream 'gcopilot-proxy' resolves via Docker DNS (container name)
  - DGX Spark runs Ollama only (port 11434) — no proxy, no dashboard
  - gcopilot-dashboard also on Databricks, uses http://gcopilot-proxy:8001 as backend

Usage:
    python scripts/deploy.py

Prerequisites:
    - SSH key configured for 'darkmatter2222@192.168.86.48' (or host alias 'databricks')
    - .env file present at repo root
"""

import os
import sys
import subprocess
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).parent.parent
DATABRICKS_HOST = "darkmatter2222@192.168.86.48"
DATABRICKS_SSH_ALIAS = "databricks"  # optional alias; falls back to full host
CONTAINER_NAME = "gcopilot-proxy"
IMAGE_NAME = "gcopilot-proxy"
PORT = 8001
DOCKER_NETWORK = "docucraft_docucraft-network"

# Files to include in the deployment archive
PROXY_FILES = [
    "proxy/main.py",
    "proxy/router.py",
    "proxy/tracker.py",
    "proxy/db.py",
    "proxy/cost_engine.py",
    "proxy/requirements.txt",
    "proxy/Dockerfile",
]


def run(cmd: str, check=True) -> subprocess.CompletedProcess:
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if result.stdout.strip():
        print(f"    {result.stdout.strip()}")
    if result.returncode != 0:
        if result.stderr.strip():
            print(f"    STDERR: {result.stderr.strip()}", file=sys.stderr)
        if check:
            print(f"  FAILED (exit {result.returncode})", file=sys.stderr)
            sys.exit(1)
    return result


def _ssh_host() -> str:
    """Return the SSH host to use — alias if reachable, otherwise full host."""
    r = subprocess.run(f"ssh -o ConnectTimeout=3 -o BatchMode=yes {DATABRICKS_SSH_ALIAS} true",
                       shell=True, capture_output=True)
    return DATABRICKS_SSH_ALIAS if r.returncode == 0 else DATABRICKS_HOST


def ssh(cmd: str, host: str = None, check=True) -> subprocess.CompletedProcess:
    h = host or DATABRICKS_HOST
    return run(f'ssh {h} "{cmd}"', check=check)


def load_env() -> dict:
    env_path = REPO / ".env"
    if not env_path.exists():
        print("ERROR: .env not found — copy .env.example to .env and fill in values",
              file=sys.stderr)
        sys.exit(1)
    env = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def build_archive() -> Path:
    """Create a .tar.gz of proxy files to deploy."""
    tmp = Path(tempfile.mkdtemp()) / "proxy-deploy.tar.gz"
    with tarfile.open(tmp, "w:gz") as tar:
        for rel in PROXY_FILES:
            full = REPO / rel
            if full.exists():
                tar.add(full, arcname=Path(rel).name)
            else:
                print(f"  WARNING: {rel} not found, skipping")
    return tmp


def main():
    print("=" * 60)
    print("  LLM Proxy — Deploy to Databricks (192.168.86.48)")
    print("=" * 60)

    env = load_env()
    # Proxy on Databricks talks to DGX Ollama over LAN
    ollama_url = env.get("DATABRICKS_OLLAMA_URL", "http://192.168.86.39:11434")
    mongo_uri  = env.get("MONGO_URI", "")
    mongo_db   = env.get("MONGO_DB", "radiacode")
    vllm_url   = env.get("VLLM_BASE_URL", "")
    min_temp   = env.get("MIN_TEMPERATURE", "0.6")
    disable_th = env.get("DISABLE_THINKING_FOR_TOOLS", "true")
    refresh_s  = env.get("ROUTER_REFRESH_S", "30")
    api_key_req = env.get("API_KEY_REQUIRED", "false")
    proxy_keys  = env.get("PROXY_API_KEYS", "")
    admin_user  = env.get("ADMIN_USERNAME", "")
    admin_pass  = env.get("ADMIN_PASSWORD", "")
    # GPU stats service on DGX Spark; auto-derived from OLLAMA_BASE_URL if not set
    gpu_stats_url = env.get("GPU_STATS_URL", "")
    gpu_mem_total = env.get("GPU_MEM_TOTAL_MB", "124928")

    host = _ssh_host()
    print(f"\nTarget  : {host} (Databricks)")
    print(f"Ollama  : {ollama_url}  (DGX Spark)")
    print(f"vLLM    : {vllm_url or '(none)'}")
    print(f"MongoDB : {'enabled' if mongo_uri else 'disabled (memory-only)'}")
    print(f"Network : {DOCKER_NETWORK}")

    # ── 1. Build archive ──
    print("\n[1/5] Building deployment archive…")
    archive = build_archive()
    print(f"  Archive: {archive} ({archive.stat().st_size // 1024} KB)")

    # ── 2. Upload to Databricks ──
    print("\n[2/5] Uploading to Databricks…")
    run(f'ssh {host} "mkdir -p ~/proxy-deploy"')
    run(f'scp "{archive}" {host}:~/proxy-deploy/proxy-deploy.tar.gz')
    ssh("cd ~/proxy-deploy && tar xzf proxy-deploy.tar.gz", host=host)
    print("  Upload complete")

    # ── 3. Build Docker image on Databricks ──
    print("\n[3/5] Building Docker image on Databricks…")
    ssh(f"cd ~/proxy-deploy && docker build --pull=false -t {IMAGE_NAME} . 2>&1 | tail -5",
        host=host)

    # ── 4. Stop old container ──
    print("\n[4/5] Stopping existing container…")
    ssh(f"docker stop {CONTAINER_NAME} 2>/dev/null || true", host=host)
    ssh(f"docker rm {CONTAINER_NAME} 2>/dev/null || true", host=host)

    # ── 5. Start new container ──
    print("\n[5/5] Starting new container…")
    env_flags = " ".join([
        f"-e OLLAMA_BASE_URL={ollama_url}",
        f"-e MIN_TEMPERATURE={min_temp}",
        f"-e DISABLE_THINKING_FOR_TOOLS={disable_th}",
        f"-e ROUTER_REFRESH_S={refresh_s}",
        f"-e API_KEY_REQUIRED={api_key_req}",
    ])
    if proxy_keys:
        env_flags += f' -e PROXY_API_KEYS="{proxy_keys}"'
    if admin_user:
        env_flags += f' -e ADMIN_USERNAME="{admin_user}"'
    if admin_pass:
        env_flags += f' -e ADMIN_PASSWORD="{admin_pass}"'
    if mongo_uri:
        env_flags += f' -e MONGO_URI="{mongo_uri}" -e MONGO_DB={mongo_db}'
    if vllm_url:
        env_flags += f" -e VLLM_BASE_URL={vllm_url}"
    if gpu_stats_url:
        env_flags += f" -e GPU_STATS_URL={gpu_stats_url}"
    if gpu_mem_total:
        env_flags += f" -e GPU_MEM_TOTAL_MB={gpu_mem_total}"

    ssh(
        f"docker run -d --name {CONTAINER_NAME} "
        f"--network {DOCKER_NETWORK} "
        f"--restart unless-stopped "
        f"-p {PORT}:{PORT} "
        f"{env_flags} "
        f"{IMAGE_NAME}",
        host=host,
    )

    # ── Verify ──
    import time
    print("\nWaiting for container to start...")
    time.sleep(5)
    result = ssh(f"curl -sf http://localhost:{PORT}/health", host=host, check=False)
    if result.returncode == 0:
        print(f"\n[OK] Deploy successful!")
        print(f"  Health    : http://192.168.86.48:{PORT}/health")
        print(f"  Models    : http://192.168.86.48:{PORT}/v1/models")
        print(f"  (via nginx): http://192.168.86.48/copilot/v1/models")
    else:
        print(f"\n  Container started but /health not responding yet.")
        print(f"  Check logs: ssh {host} docker logs {CONTAINER_NAME}")

    archive.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
