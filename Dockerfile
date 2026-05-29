# === Build stage ===
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*
# libxcb1 added in 0.1.2 — docling's PDF renderer needs it for table
# extraction. Pre-0.1.2 every PDF ingest emitted an ImportError and silently
# fell back to pypdf (functional but noisy in setup logs).

COPY pyproject.toml README.md ./
# 0.1.2: install CPU-only torch FIRST so [backend]'s sentence-transformers
# doesn't pull the 2-4 GB of nvidia-cu* libs as transitive deps. M1 build
# was burning ~20 min downloading CUDA libraries that ARM64 can't use.
# Once torch is satisfied from the CPU index, .[backend] sees it as
# already-installed and skips the GPU variant.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch torchvision

# Backend deps are gated behind the [backend] extra as of 0.1.1 so the
# PyPI wheel stays lean for CLI installers. The api container needs the
# full backend toolchain, so we explicitly install with [backend].
RUN pip install --no-cache-dir ".[backend]"

# === Runtime stage ===
FROM python:3.12-slim

# Runtime needs libxcb1 too (the build-stage install only landed in /usr/lib
# of the builder image; we're copying just site-packages + scripts across).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

LABEL maintainer="Rishabh" \
      description="FinanceBench RAG Agent API" \
      version="0.1.4"

WORKDIR /app

# Create non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ src/
COPY scripts/ scripts/
COPY data/sample/ data/sample/
# 0.1.3: alembic.ini + migrations/ are required by src.api.main lifespan to
# run schema migrations on boot. Without them the lifespan logs "Alembic
# upgrade failed: No 'script_location' key found in configuration." and
# falls back to static RBAC. Non-fatal but means new migrations don't apply
# in the container. (script_location = migrations per alembic.ini.)
COPY alembic.ini alembic.ini
COPY migrations/ migrations/

# Change ownership and switch to non-root
RUN chown -R appuser:appuser /app
# 0.1.4: pre-create the HuggingFace cache directory with appuser ownership
# BEFORE the USER switch and BEFORE the hf_cache volume mounts over it. On
# first mount of an empty named volume, Docker copies the in-image directory's
# permissions into the new volume. Without this step, the volume is created
# root-owned, appuser can't write, BGE/docling downloads fail with PermissionError,
# and sentence-transformers ends up loading a partial model cache that raises
# "Unrecognized model in BAAI/bge-reranker-v2-m3". (0.1.3 M1 hit this.)
RUN mkdir -p /home/appuser/.cache/huggingface && \
    chown -R appuser:appuser /home/appuser/.cache
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
