#!/usr/bin/env python3
"""
Restart DGX Spark proxy container with MongoDB env vars.
Usage:
  $env:DGXSPARK_SUDO_PASS='password'
  python scripts/fix_mongo_only.py
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
    print("Need the DGX Spark sudo password to restart Docker:")
    SUDO_PASS = input("  Password: ").strip()
    if not SUDO_PASS:
        print("No password given, aborting.")
        sys.exit(1)

print(f"Connecting to {HOST}...")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(hostname=HOST, username=USER, key_filename=SSH_KEY)
print("Connected OK\n")

def sudo_cmd(c, cmd_str, timeout=60):
    full = f"echo '{SUDO_PASS}' | sudo -S bash -c '{cmd_str}'"
    _, out, err = c.exec_command(full, timeout=timeout)
    return out.read().decode(), err.read().decode()

# Stop and remove old container
print("Stopping old container...")
out, _ = sudo_cmd(client, "docker stop gcopilot-proxy 2>/dev/null; docker rm -f gcopilot-proxy 2>/dev/null")
if out.strip():
    print(out.strip())

# Build new docker run command
uri_safe = MONGO_URI.replace("'", "'\\''")
run_cmd = (
    "docker run -d "
    "--name gcopilot-proxy "
    "--restart unless-stopped "
    "--network host "
    "-e OLLAMA_BASE_URL=http://localhost:11434 "
    "-e SERVED_MODEL_NAME=qwen3 "
    "-e MIN_TEMPERATURE=0.6 "
    f"-e MONGO_URI='{uri_safe}' "
    f"-e MONGO_DB={MONGO_DB} "
    "gcopilot-proxy"
)

print(f"\nStarting with MongoDB...")
out, err = sudo_cmd(client, run_cmd, timeout=30)
if out.strip():
    print(f"  {out.strip()[:64]}")
elif err.strip():
    print(f"  Error: {err.strip()[:200]}")

time.sleep(5)

# Verify
print("\nContainer status:")
out, _ = sudo_cmd(client, "docker ps --format '{{.Names}} {{.Status}}'")
print(f"  {out.strip()}")

print("\nHealth check:")
_, out2, _ = client.exec_command("curl -s http://localhost:8001/health", timeout=10)
print(f"  {out2.read().decode().strip()}")

print("\nMongoDB log lines:")
out3, _ = sudo_cmd(client, "docker logs gcopilot-proxy 2>&1 | grep -i mongo | tail -5")
for line in out3.strip().split('\n'):
    if line:
        print(f"  {line}")

print("\nDone!")
client.close()
