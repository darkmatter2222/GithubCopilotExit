#!/usr/bin/env python3
"""
Deploy the LLM Proxy Dashboard to Data Brick (192.168.86.48).

This replaces the manual docker-run steps in AGENTS.md with a repeatable,
.env-driven script — avoiding stale/incorrect credentials from hand-typed
docker commands (e.g. shell history expansion mangling "!" in passwords).

Architecture:
  - gcopilot-dashboard runs on Data Brick, on docucraft_docucraft-network
  - Talks to gcopilot-proxy via Docker DNS name (http://gcopilot-proxy:8001)
  - Exposed to LAN/internet via nginx ingress at /copilot/ (see nginx/current_nginx.conf)

Usage:
    python scripts/deploy_dashboard.py

Prerequisites:
    - SSH key configured for 'darkmatter2222@192.168.86.48' (or host alias 'databricks')
    - .env file present at repo root with DASHBOARD_USERNAME/PASSWORD, PROXY_API_KEY, etc.
"""

import sys
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
DATABRICK_HOST = "darkmatter2222@192.168.86.48"
DATABRICK_SSH_ALIAS = "databricks"  # matches ~/.ssh/config Host alias
CONTAINER_NAME = "gcopilot-dashboard"
IMAGE_NAME = "gcopilot-dashboard"
PORT = 3002
DOCKER_NETWORK = "docucraft_docucraft-network"

DASHBOARD_FILES = [
    "dashboard/index.html",
    "dashboard/serve.py",
    "dashboard/Dockerfile.deploy",
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
    r = subprocess.run(f"ssh -o ConnectTimeout=3 -o BatchMode=yes {DATABRICK_SSH_ALIAS} true",
                       shell=True, capture_output=True)
    return DATABRICK_SSH_ALIAS if r.returncode == 0 else DATABRICK_HOST


def ssh(cmd: str, host: str = None, check=True) -> subprocess.CompletedProcess:
    h = host or DATABRICK_HOST
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
    tmp = Path(tempfile.mkdtemp()) / "dashboard-deploy.tar.gz"
    with tarfile.open(tmp, "w:gz") as tar:
        for rel in DASHBOARD_FILES:
            full = REPO / rel
            if full.exists():
                tar.add(full, arcname=Path(rel).name)
            else:
                print(f"  WARNING: {rel} not found, skipping")
    return tmp


def _remote_env_file(env: dict) -> str:
    """Build a remote-side env file (KEY=VALUE per line) to avoid any shell
    quoting/history-expansion issues with special characters like '!' in
    passwords when passed as inline `-e` docker flags."""
    pairs = {
        "PROXY_BACKEND": env.get("PROXY_BACKEND_DASHBOARD", "http://gcopilot-proxy:8001"),
        "PROXY_API_KEY": env.get("PROXY_API_KEY", ""),
        "ADMIN_USERNAME": env.get("ADMIN_USERNAME", ""),
        "ADMIN_PASSWORD": env.get("ADMIN_PASSWORD", ""),
        "DASHBOARD_USERNAME": env.get("DASHBOARD_USERNAME", ""),
        "DASHBOARD_PASSWORD": env.get("DASHBOARD_PASSWORD", ""),
        "DASHBOARD_PORT": str(PORT),
        "PROXY_PATH_PREFIX": env.get("PROXY_PATH_PREFIX", "/copilot"),
    }
    return "\n".join(f"{k}={v}" for k, v in pairs.items() if v != "") + "\n"


def main():
    print("=" * 60)
    print("  LLM Proxy Dashboard — Deploy to Data Brick (192.168.86.48)")
    print("=" * 60)

    env = load_env()
    host = _ssh_host()
    print(f"\nTarget  : {host} (Data Brick)")
    print(f"Network : {DOCKER_NETWORK}")

    # ── 1. Build archive ──
    print("\n[1/6] Building deployment archive…")
    archive = build_archive()
    print(f"  Archive: {archive} ({archive.stat().st_size // 1024} KB)")

    # ── 2. Upload archive + env file ──
    print("\n[2/6] Uploading to Data Brick…")
    run(f'ssh {host} "mkdir -p ~/dashboard-deploy"')
    run(f'scp "{archive}" {host}:~/dashboard-deploy/dashboard-deploy.tar.gz')
    ssh("cd ~/dashboard-deploy && tar xzf dashboard-deploy.tar.gz", host=host)

    # Write the env file locally then scp it up — avoids any shell escaping
    # issues (e.g. bash history expansion eating '!' in passwords).
    env_file_content = _remote_env_file(env)
    tmp_env = Path(tempfile.mkdtemp()) / "gcopilot-dashboard.env"
    tmp_env.write_text(env_file_content, newline="\n")
    run(f'scp "{tmp_env}" {host}:~/dashboard-deploy/gcopilot-dashboard.env')
    print("  Upload complete")

    # ── 3. Build Docker image ──
    print("\n[3/6] Building Docker image on Data Brick…")
    ssh(f"cd ~/dashboard-deploy && docker build --no-cache -f Dockerfile.deploy "
        f"-t {IMAGE_NAME} . 2>&1 | tail -10", host=host)

    # ── 4. Stop old container ──
    print("\n[4/6] Stopping existing container…")
    ssh(f"docker stop {CONTAINER_NAME} 2>/dev/null || true", host=host)
    ssh(f"docker rm {CONTAINER_NAME} 2>/dev/null || true", host=host)

    # ── 5. Start new container using --env-file (safe for special chars) ──
    print("\n[5/6] Starting new container…")
    ssh(
        f"docker run -d --name {CONTAINER_NAME} "
        f"--restart unless-stopped "
        f"--network {DOCKER_NETWORK} "
        f"-p {PORT}:{PORT} "
        f"--env-file ~/dashboard-deploy/gcopilot-dashboard.env "
        f"{IMAGE_NAME}",
        host=host,
    )

    # ── 6. Verify ──
    print("\n[6/6] Verifying deployment...")
    time.sleep(5)
    result = ssh(f"curl -sf http://localhost:{PORT}/healthcheck", host=host, check=False)
    if result.returncode == 0:
        print("\n[OK] Deploy successful!")
        print(f"  Healthcheck : http://192.168.86.48:{PORT}/healthcheck")
        print(f"  Via nginx   : https://192.168.86.48/copilot/")
        print(f"  Public      : https://susmannet.duckdns.org/copilot/")
    else:
        print("\n  Container started but /healthcheck not responding yet.")
        print(f"  Check logs: ssh {host} docker logs {CONTAINER_NAME}")

    # Clean up the temp env file locally (contains secrets — do not leave on disk).
    tmp_env.unlink(missing_ok=True)
    archive.unlink(missing_ok=True)
    # Remove the remote env file too now that the container has read it into
    # its own environment — it doesn't need to persist on disk on the host.
    ssh(f"shred -u ~/dashboard-deploy/gcopilot-dashboard.env 2>/dev/null || "
        f"rm -f ~/dashboard-deploy/gcopilot-dashboard.env", host=host, check=False)


if __name__ == "__main__":
    main()
