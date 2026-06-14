#!/bin/bash
# Update Ollama to latest version and restart container with new model
# No sudo required — downloads tarball, extracts binary, COPYs into Docker image.
set -e

echo "==> Finding latest Ollama release..."
TAG=$(curl -fsSL https://api.github.com/repos/ollama/ollama/releases/latest | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")
echo "    Latest tag: $TAG"

TARBALL_URL="https://github.com/ollama/ollama/releases/download/${TAG}/ollama-linux-amd64.tar.zst"
echo "==> Downloading $TARBALL_URL (~1.3 GB, please wait)..."
mkdir -p /tmp/ollama-build
curl -fsSL "$TARBALL_URL" -o /tmp/ollama-build/ollama-linux-amd64.tar.zst

echo "==> Extracting ollama binary from tarball..."
cd /tmp/ollama-build
zstd -d ollama-linux-amd64.tar.zst --stdout | tar -x --strip-components=2 -f - ./bin/ollama 2>/dev/null \
    || zstd -d ollama-linux-amd64.tar.zst --stdout | tar -x -f - --wildcards '*/ollama' --strip-components=2 2>/dev/null \
    || (zstd -d ollama-linux-amd64.tar.zst -o ollama.tar && tar -xf ollama.tar --wildcards '*/ollama' --strip-components=2 && rm ollama.tar)

chmod +x /tmp/ollama-build/ollama
echo "    Version: $(/tmp/ollama-build/ollama --version)"

echo "==> Building updated Ollama Docker image..."
cat > /tmp/ollama-build/Dockerfile << 'EOF'
FROM ollama/ollama:latest
COPY ollama /usr/local/bin/ollama
RUN chmod +x /usr/local/bin/ollama
EOF

docker build -t ollama/ollama:updated /tmp/ollama-build

echo "==> Starting updated Ollama container..."
docker rm -f ollama 2>/dev/null || true
docker run -d \
    --name ollama \
    --gpus all \
    -v ollama:/root/.ollama \
    -p 11434:11434 \
    --restart unless-stopped \
    ollama/ollama:updated

echo "==> Waiting for Ollama to be ready..."
for i in $(seq 1 20); do
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "    Ollama is ready!"
        break
    fi
    echo "    Waiting ($i/20)..."
    sleep 2
done

echo "==> Verifying version inside container..."
docker exec ollama ollama --version

echo "==> Pulling qwen3.6:27b-mtp-q4_K_M model..."
docker exec ollama ollama pull qwen3.6:27b-mtp-q4_K_M

echo "==> Models installed:"
docker exec ollama ollama list

echo "Done!"
