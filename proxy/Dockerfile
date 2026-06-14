FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all proxy source files (main.py, tracker.py, etc.)
COPY *.py ./

ENV OLLAMA_BASE_URL=http://ollama:11434
ENV SERVED_MODEL_NAME=qwen3
ENV MIN_TEMPERATURE=0.6

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
