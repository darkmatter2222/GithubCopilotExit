#!/usr/bin/env python3
"""Download Qwen3.6-27B-FP8 and Qwen3-Coder-30B-A3B-Instruct-FP8 from HuggingFace."""
import sys
from huggingface_hub import snapshot_download

models = [
    ("Qwen/Qwen3.6-27B-FP8", "/home/darkmatter2222/models/qwen3.6-27b-fp8"),
    ("Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8", "/home/darkmatter2222/models/qwen3-coder-30b-fp8"),
]

target = sys.argv[1] if len(sys.argv) > 1 else "all"

for repo_id, local_dir in models:
    name = repo_id.split("/")[1]
    if target != "all" and target not in name:
        continue
    print(f"\n=== Downloading {repo_id} -> {local_dir} ===", flush=True)
    try:
        path = snapshot_download(
            repo_id=repo_id,
            local_dir=local_dir,
        )
        print(f"=== Done: {path} ===", flush=True)
    except Exception as e:
        print(f"=== ERROR: {e} ===", flush=True)
        sys.exit(1)

print("\nAll downloads complete.", flush=True)
