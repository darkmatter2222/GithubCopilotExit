#!/usr/bin/env bash
set -e

# Activate uv env
source ~/.local/bin/env 2>/dev/null || true
source ~/.venvs/vllm/bin/activate

export HF_HOME=~/Models

# Path to Qwen3.6-27B GGUF — download via:
#   huggingface-cli download unsloth/Qwen3.6-27B-MTP-GGUF \
#     Qwen3.6-27B-MTP-Q4_K_M.gguf --local-dir ~/Models/Qwen3.6-27B
GGUF="$HOME/Models/Qwen3.6-27B/Qwen3.6-27B-MTP-Q4_K_M.gguf"

exec python -m vllm.entrypoints.openai.api_server \
    --model "$GGUF" \
    --load-format gguf \
    --tokenizer Qwen/Qwen3.6-27B \
    --host 0.0.0.0 \
    --port 8000 \
    --served-model-name qwen3 \
    --gpu-memory-utilization 0.92 \
    --max-model-len 262144 \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --trust-remote-code
