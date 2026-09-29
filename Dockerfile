# syntax=docker/dockerfile:1
# Based on your original Dockerfile. Changes are marked "CHANGED".
FROM python:3.12-slim AS deps
# CHANGED: 3.14 -> 3.12 (widest wheel support for onnxruntime / tokenizers / pydantic-core)

# System packages:
#   tesseract-ocr(-eng) — local OCR (tools/ocr_tool.py)
#   poppler-utils       — pdftoppm for pdf2image
#   libgomp1            — OpenMP runtime, required by onnxruntime (fastembed) and numpy
#   gcc / g++           — only used if a package has no prebuilt wheel
RUN apt-get update && apt-get install -y --no-install-recommends \
  tesseract-ocr \
  tesseract-ocr-eng \
  poppler-utils \
  libgomp1 \
  gcc \
  g++ \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CHANGED: cache location for the embedding model, baked into the image below
ENV PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/app/.fastembed_cache

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r requirements.txt

# CHANGED: download the ~90 MB embedding model at BUILD time so a Render
# cold start (ephemeral disk) doesn't have to download it again.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2', cache_dir='/app/.fastembed_cache')"

FROM deps AS runtime
WORKDIR /app
COPY . .
RUN mkdir -p /tmp/ledgerlens_scratch

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('PORT','8000'))" \
  || exit 1

# CHANGED: bind to $PORT (Render injects it), default 8000.
# --workers 1 stays: MemorySaver is in-memory, and it keeps RAM low.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
