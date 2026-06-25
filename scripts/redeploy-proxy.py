#!/usr/bin/env python3
"""
Upload updated proxy files, rebuild Docker image, restart gcopilot-proxy on DGX Spark.
Usage:
  $env:DGXSPARK_SUDO_PASS='password'
  python scripts/redeploy-proxy.py
  # Or run without env var — will prompt interactively.
"""
import os
import sys
import time
import paramiko
from dotenv import load_dotenv

load_dotenv()

HOST = "dgxspark"
USER = "darkmatter2222"
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
SUDO_PASS = os.environ.get("DGXSPARK_SUDO_PASS", "")
MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB = os.environ.get("MONGO_DB", "radiacode")

if not SUDO_PASS:
    print("Need the DGX Spark sudo password to rebuild/restart Docker:")
    SUDO_PASS = input("  Password: ").strip()
    if not SUDO_PASS:
        print("No password given, aborting.")
        sys.exit(1)

print(f"Connecting to {HOST}...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname=HOST, username=USER, key_filename=SSH_KEY)
print("Connected OK\n")


def sudo_cmd(c, cmd_str, timeout=180):
    full = f"echo '{SUDO_PASS}' | sudo -S bash -c '{cmd_str}'"
    _, out, err = c.exec_command(full, timeout=timeout)
    output = out.read().decode().strip()
    error = err.read().decode().strip()
    if output:
        print(output)
    if error and "password" not in error.lower() and "[sudo]" not in error.lower():
        print(f"  STDERR: {error}")
    return out.channel.recv_exit_status()


def plain_cmd(c, cmd_str, timeout=60):
    _, out, err = c.exec_command(cmd_str, timeout=timeout)
    output = out.read().decode().strip()
    error = err.read().decode().strip()
    if output:
        print(output)
    if error:
        print(f"  STDERR: {error}")
    return out.channel.recv_exit_status()


# ── Upload updated proxy source files ────────────────────────────────
remote_dir = f"/home/{USER}/gcopilot-proxy"
local_proxy = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy")

print("--- Uploading proxy source files ---")
sftp = client.open_sftp()
for fname in os.listdir(local_proxy):
    local_path = os.path.join(local_proxy, fname)
    if os.path.isfile(local_path):
        remote_path = f"{remote_dir}/{fname}"
        sftp.put(local_path, remote_path)
        print(f"  Uploaded: {fname}")
sftp.close()

# ── Rebuild Docker image ──────────────────────────────────────────────
print("\n--- Building Docker image ---")
sudo_cmd(client, f"cd {remote_dir} && docker build -t gcopilot-proxy .", timeout=300)

# ── Stop old container ────────────────────────────────────────────────
print("\n--- Stopping old container ---")
sudo_cmd(client, "docker stop gcopilot-proxy 2>/dev/null; docker rm -f gcopilot-proxy 2>/dev/null", timeout=30)

# ── Start new container with keep_alive env var ───────────────────────
print("\n--- Starting new container ---")
uri_safe = MONGO_URI.replace("'", "'\\''")
run_cmd = (
    "docker run -d "
    "--name gcopilot-proxy "
    "--restart unless-stopped "
    "--network host "
    "-e VLLM_BASE_URL=http://localhost:8000 "
    "-e VLLM_CODER_BASE_URL=http://localhost:8002 "
    "-e SERVED_MODEL_NAME=qwen3 "
    "-e MIN_TEMPERATURE=0.6 "
    f"-e MONGO_URI='{uri_safe}' "
    f"-e MONGO_DB={MONGO_DB} "
    "gcopilot-proxy"
)
sudo_cmd(client, run_cmd, timeout=30)

# ── Verify health ─────────────────────────────────────────────────────
print("\n--- Waiting for proxy to start ---")
time.sleep(4)
rc = plain_cmd(client, "curl -sf http://localhost:8001/health")
if rc == 0:
    print("\nProxy redeployed successfully with keep_alive injection.")
else:
    print("\nHealth check failed — check /tmp/docker.log on DGX Spark.")
    sudo_cmd(client, "docker logs gcopilot-proxy --tail 20")

client.close()
