# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------
# Stage 1 — dependency layer (cached separately from source code)
# -----------------------------------------------------------------------
FROM python:3.14-slim AS deps

# System packages required at runtime:
#   tesseract-ocr  — local OCR for scanned invoices (tools/ocr_tool.py)
#   poppler-utils  — pdftoppm binary used by pdf2image (extraction_agent.py)
#   libgomp1       — OpenMP runtime required by sentence-transformers / numpy
#   gcc / g++      — needed to compile any packages that ship without a wheel
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
        libgomp1 \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only the requirements file first so this layer is cached as long
# as requirements.txt hasn't changed, even when source code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------
# Stage 2 — runtime image
# -----------------------------------------------------------------------
FROM deps AS runtime

WORKDIR /app

# Copy the full source tree.
# .dockerignore prevents .venv, __pycache__, .env, uv.lock, etc. from
# being copied in — see .dockerignore at the repo root.
COPY . .

# Temp directory for PDF scratch files used during graph execution.
# api.py resolves this via tempfile.gettempdir() so no explicit mkdir
# is needed, but creating it here makes the intent explicit.
RUN mkdir -p /tmp/ledgerlens_scratch

# Expose the port uvicorn will bind to.
EXPOSE 8000

# Healthcheck — Render / ECS / Docker Compose can use this.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
  || exit 1

# Run the FastAPI server.
# --host 0.0.0.0 is required so the port is reachable from outside the
# container.  Workers=1 is intentional: LangGraph's MemorySaver is an
# in-memory checkpointer — more than one worker means different workers
# can't see each other's pending approval states.  Switch to a
# Postgres-backed checkpointer before bumping workers.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
