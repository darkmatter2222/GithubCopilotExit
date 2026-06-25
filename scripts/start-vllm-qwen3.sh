#!/bin/bash
KWARGS='{"enable_thinking": false}'
exec /home/darkmatter2222/vllm-env/bin/vllm serve \
  /home/darkmatter2222/models/qwen3.6-27b-fp8 \
  --served-model-name qwen3 \
  --host 0.0.0.0 \
  --port 8000 \
  --max-model-len 131072 \
  --gpu-memory-utilization 0.40 \
  --max-num-seqs 4 \
  --enable-prefix-caching \
  --default-chat-template-kwargs "$KWARGS" \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser qwen3 \
  --language-model-only
