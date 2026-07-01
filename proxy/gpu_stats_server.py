#!/usr/bin/env python3
"""
GPU Stats Server - lightweight HTTP service that exposes real-time GPU metrics.

Run this on DGX Spark alongside Ollama so the gcopilot-proxy on Data Brick
(which has no local GPU) can read real-time GPU utilization and VRAM stats.

For the GB10 Grace Blackwell (unified memory), nvidia-smi reports [N/A] for
memory fields. This server merges two sources:
  - nvidia-smi  -> util_percent, temp_c
  - Ollama /api/ps -> mem_used_mb (size_vram of loaded model)
  - GPU_MEM_TOTAL_MB env -> mem_total_mb (default: 124928 MB = 122 GB for GB10)

Usage:
    python3 gpu_stats_server.py          # listens on port 11435
    GPU_STATS_PORT=11435 python3 gpu_stats_server.py

Systemd quick-setup (run as root on DGX Spark):
    sudo cp gpu_stats_server.py /usr/local/bin/gpu_stats_server.py
    sudo cp gpu-stats.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now gpu-stats

Responds to:
    GET /gpu-stats  -> {"gpus": [{name, util_percent, mem_used_mb, mem_total_mb, temp_c}]}
    GET /health     -> same (for monitoring)
"""

import json
import os
import subprocess
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("GPU_STATS_PORT", sys.argv[1] if len(sys.argv) > 1 else "11435"))
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
GPU_MEM_TOTAL_MB = int(os.environ.get("GPU_MEM_TOTAL_MB", "124928"))  # GB10 = 122 GB


def _i(s: str) -> int:
    """Parse nvidia-smi integer field, returning 0 for N/A or errors."""
    c = s.strip().strip("[]")
    try:
        return int(float(c)) if c and c.upper() not in ("N/A", "") else 0
    except ValueError:
        return 0


def query_nvidia_smi() -> list:
    """Return list of GPU dicts from nvidia-smi (util%, temp; memory may be 0 on GB10)."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        gpus = []
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "name": parts[0],
                    "util_percent": _i(parts[1]),
                    "mem_used_mb": _i(parts[2]),
                    "mem_total_mb": _i(parts[3]),
                    "temp_c": _i(parts[4]),
                })
        return gpus
    except Exception as e:
        print(f"nvidia-smi error: {e}", flush=True)
        return []


def query_ollama_vram() -> int:
    """Return total VRAM used by all Ollama loaded models (bytes -> MB), or 0."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/ps", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        total_vram = sum(m.get("size_vram", 0) for m in data.get("models", []))
        return total_vram // (1024 * 1024)
    except Exception:
        return 0


def get_gpu_stats() -> list:
    """Merge nvidia-smi (util/temp) with Ollama size_vram (memory)."""
    gpus = query_nvidia_smi()
    if not gpus:
        return []

    # On GB10 unified memory, nvidia-smi returns [N/A] for memory fields.
    # Fill from Ollama /api/ps instead.
    needs_vram = any(g["mem_total_mb"] == 0 for g in gpus)
    if needs_vram:
        vram_used_mb = query_ollama_vram()
        for g in gpus:
            if g["mem_total_mb"] == 0:
                g["mem_used_mb"] = vram_used_mb
                g["mem_total_mb"] = GPU_MEM_TOTAL_MB

    return gpus


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/gpu-stats", "/health"):
            gpus = get_gpu_stats()
            body = json.dumps({"gpus": gpus}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # Suppress per-request access logs


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"GPU stats server listening on 0.0.0.0:{PORT}", flush=True)
    print(f"  nvidia-smi -> util%, temp_c", flush=True)
    print(f"  Ollama {OLLAMA_URL} -> size_vram (GB10 unified memory)", flush=True)
    print(f"  GPU total: {GPU_MEM_TOTAL_MB} MB ({GPU_MEM_TOTAL_MB//1024} GB)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass