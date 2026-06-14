#!/usr/bin/env bash
# fix-context.sh — Restart Ollama with 262K context + recreate qwen3 alias
set -e

echo "==> Stopping and removing existing ollama container..."
docker stop ollama 2>/dev/null || true
docker rm ollama 2>/dev/null || true

echo "==> Starting ollama with OLLAMA_KEEP_ALIVE=-1 and OLLAMA_NUM_CTX=262144..."
docker run -d \
    --name ollama \
    --gpus all \
    -v ollama:/root/.ollama \
    -p 11434:11434 \
    --restart unless-stopped \
    -e OLLAMA_KEEP_ALIVE=-1 \
    -e OLLAMA_NUM_CTX=262144 \
    ollama/ollama:v0.30.8-final

echo "==> Waiting for Ollama to be ready..."
for i in $(seq 1 20); do
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "    Ready!"
        break
    fi
    echo "    Waiting ($i/20)..."
    sleep 2
done

echo "==> Creating qwen3 alias with 262K context..."
# Write the Modelfile inside the container and create alias from there
docker exec ollama bash -c 'printf "FROM qwen3.6:27b-mtp-q4_K_M\nPARAMETER num_ctx 262144\n" > /tmp/qwen3.modelfile && ollama create qwen3 -f /tmp/qwen3.modelfile'

echo "==> Verifying alias..."
docker exec ollama ollama list

echo "==> Warming up model into VRAM..."
python3 /tmp/warmup.py

echo ""
echo "==> VRAM usage:"
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader

echo ""
echo "==> Loaded models and context length:"
curl -s http://localhost:11434/api/ps

echo ""
echo "Done! Model is hot in VRAM with 262K context."
