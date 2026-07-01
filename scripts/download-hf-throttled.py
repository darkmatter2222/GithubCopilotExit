#!/usr/bin/env python3
"""
Throttled HuggingFace model file downloader.

Uses huggingface_hub to resolve the CDN URL with auth token,
then wget --limit-rate for throttled download. Supports resume.

Usage:
    python3 scripts/download-hf-throttled.py \\
        ocicek/Qwen3.6-27B-NVFP4 model-llm-nvfp4.safetensors \\
        /path/to/dest --max-rate 25M
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi


def get_token() -> str:
    """Get HF auth token from env or cached credentials."""
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    if token:
        return token
    # Try to read from HF cache
    hf_home = Path.home() / ".cache" / "huggingface"
    token_file = hf_home / "token"
    if token_file.exists():
        return token_file.read_text().strip()
    # Try .netrc as fallback
    netrc = Path.home() / ".netrc"
    if netrc.exists():
        import re
        text = netrc.read_text()
        match = re.search(r'machine huggingface\.co.*?password\s+(\S+)', text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def get_signed_url(repo_id: str, filename: str) -> tuple[str, int]:
    """
    Resolve the CDN URL for a HF file using cache metadata.
    Returns (url, expected_size_bytes).
    """
    api = HfApi()
    
    # Get file meta to resolve CDN URL
    rinfo = api.get_paths_info(repo_id, filename)[0]
    size = rinfo.size if hasattr(rinfo, 'size') else 0
    
    # Build direct download URL with token
    token = get_token()
    url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
    
    return url, size


def download_throttled(url: str, output: str, max_rate: str, cookie_file: str) -> bool:
    """Download using wget with rate limiting and resume support."""
    cmd = [
        "wget",
        "--continue",           # resume partial downloads  
        "--tries", "8",
        "--waitretry", "10",
        "--timeout", "45",
        "--dns-timeout", "30",
        "--connect-timeout", "30",
        "--read-timeout", "120",
        "--limit-rate", max_rate,
        "--content-disposition",
        "-O", output,
    ]
    
    # Use cookie file for auth instead of token in URL
    if cookie_file and os.path.exists(cookie_file):
        cmd += ["--load-cookies", cookie_file]
    else:
        cmd += ["--header", "Authorization: Bearer " + get_token()]
    
    cmd += [url]
    
    print(f"\n{'='*70}")
    print(f"URL  : {url[:180]}...")
    print(f"To   : {output}")
    print(f"Rate : max {max_rate}")
    print(f"{'='*70}\n")
    
    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Throttled HF model file downloader")
    parser.add_argument("repo", help="HF repo ID (e.g. ocicek/Qwen3.6-27B-NVFP4)")
    parser.add_argument("file", help="Filename to download")
    parser.add_argument("dest", help="Destination directory")
    parser.add_argument("--max-rate", default="25M",
        help="Max rate for wget (default: 25M ~200Mbps)")
    args = parser.parse_args()

    os.makedirs(args.dest, exist_ok=True)
    target = os.path.join(args.dest, args.file)
    
    # Check if already fully downloaded
    if os.path.exists(target):
        sz = os.path.getsize(target)
        print(f"File exists: {target} ({sz / (1024**3):.1f} GB)")
        if sz > 1e9:
            print("Looks complete, skipping.")
            return
    
    # Clean up any incomplete cache files from previous attempts
    cache_dir = os.path.join(args.dest, ".cache")
    if os.path.exists(cache_dir):
        print(f"Clearing stale cache...")
        import shutil
        shutil.rmtree(cache_dir)
    
    print(f"Resolving CDN URL for {args.repo}/{args.file} ...")
    try:
        url, expected = get_signed_url(args.repo, args.file)
        if expected:
            print(f"Expected size: {expected / (1024**3):.1f} GB")
        
        while True:
            success = download_throttled(url, target, args.max_rate, None)
            sz = os.path.getsize(target) if os.path.exists(target) else 0
            print(f"\nAttempt result: {'OK' if success else 'FAILED'} (file: {sz/(1024**3):.1f} GB)")
            
            if success or sz >= expected * 0.95:
                break
            answer = input("Download incomplete. Retry? [Y/n] ").strip().lower()
            if answer in ("n", "no"):
                print("Aborted.")
                return
            
            # Re-resolve URL (tokens expire)
            url, _ = get_signed_url(args.repo, args.file)
            time.sleep(5)
        
        print(f"\nDone: {target}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

