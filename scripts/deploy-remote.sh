#!/usr/bin/env bash
# deploy-remote.sh — Deploy qwen3.6:27b-mtp-q4_K_M + temperature proxy to remote RTX 3090 server.
# Reads SSH_USER, SSH_HOST, SSH_KEY_PATH from .env in repo root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env
if [[ -f "$REPO_ROOT/.env" ]]; then
    # shellcheck disable=SC1090
    set -a; source "$REPO_ROOT/.env"; set +a
fi

SSH_USER="${SSH_USER:-darkmatter2222}"
SSH_HOST="${SSH_HOST:-192.168.86.48}"
SSH_KEY="${SSH_KEY_PATH:-~/.ssh/id_rsa}"
REMOTE_DEPLOY_DIR="/home/$SSH_USER/llm-stack"
PROXY_PORT="${PROXY_PORT:-8001}"       # same port atomic-family-llm used
MODEL="qwen3.6:27b-mtp-q4_K_M"
SERVED_NAME="qwen3"

SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=no"

echo "==> Copying proxy source to $SSH_HOST:$REMOTE_DEPLOY_DIR"
ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" "mkdir -p $REMOTE_DEPLOY_DIR"
scp $SSH_OPTS \
    "$REPO_ROOT/proxy/main.py" \
    "$REPO_ROOT/proxy/requirements.txt" \
    "$REPO_ROOT/proxy/Dockerfile" \
    "$SSH_USER@$SSH_HOST:$REMOTE_DEPLOY_DIR/"

echo "==> Building proxy image on remote"
ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" \
    "cd $REMOTE_DEPLOY_DIR && docker build -t llm-proxy:latest ."

echo "==> Pulling $MODEL into remote Ollama"
ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" \
    "docker exec ollama ollama pull $MODEL"

echo "==> Stopping atomic-family-llm (frees VRAM)"
ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" \
    "docker stop atomic-family-llm 2>/dev/null || true"

echo "==> Starting temperature proxy on port $PROXY_PORT"
ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" "
docker rm -f llm-proxy 2>/dev/null || true
docker run -d \
    --name llm-proxy \
    --network host \
    --restart unless-stopped \
    -e OLLAMA_BASE_URL=http://localhost:11434 \
    -e SERVED_MODEL_NAME=$SERVED_NAME \
    -e MIN_TEMPERATURE=0.6 \
    -p ${PROXY_PORT}:8000 \
    --health-cmd='python3 -c \"import urllib.request; urllib.request.urlopen(\\\"http://localhost:8000/health\\\")\"' \
    --health-interval=30s \
    --health-timeout=5s \
    --health-start-period=15s \
    llm-proxy:latest
"

echo ""
echo "==> Waiting for proxy to become healthy..."
for i in {1..20}; do
    STATUS=$(ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" \
        "docker inspect --format='{{.State.Health.Status}}' llm-proxy 2>/dev/null || echo unknown")
    if [[ "$STATUS" == "healthy" ]]; then
        break
    fi
    echo "    $i/20 status: $STATUS"
    sleep 3
done

echo ""
echo "==> Smoke test — listing models via proxy"
ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" \
    "curl -sf http://localhost:${PROXY_PORT}/v1/models | python3 -m json.tool"

echo ""
echo "==> Smoke test — temperature clamp check (sending 0.1, should be clamped to 0.6)"
ssh $SSH_OPTS "$SSH_USER@$SSH_HOST" "
curl -sf http://localhost:${PROXY_PORT}/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"$SERVED_NAME\",\"temperature\":0.1,\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"Reply OK\"}]}' \
  | python3 -m json.tool | head -30
"

echo ""
echo "Done!"
echo "  Endpoint : http://$SSH_HOST:$PROXY_PORT/v1"
echo "  Model    : $SERVED_NAME"
echo "  Temp min : 0.6 (clamped)"
echo "  Tools    : enabled (native Ollama)"
echo "  Context  : 256K (262144 tokens)"
