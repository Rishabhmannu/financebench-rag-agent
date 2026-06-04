# === Build stage ===
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
# 0.3.1: dropped libxcb1 + libgl1 (~64 MB compressed). 0.1.2/0.1.5 installed
# them for docling's OpenCV-based table extraction. 0.3.1 moves docling to
# the optional [docling] extra; default install uses pypdf only (the
# canonical parser anyway). Users running `pip install ".[docling]"` need
# to install libxcb1 + libgl1 themselves (apt-get on Debian/Ubuntu, brew on
# macOS) — out of scope for the default image.

# Deps are installed from a version-free manifest (mirrors pyproject's
# base + [backend] deps) rather than from pyproject.toml itself, so a
# version-only bump does NOT invalidate this layer. Before this, COPYing the
# version-bearing pyproject.toml ahead of the install meant every release
# changed the layer digest and forced `financebench upgrade` to re-pull the
# full ~555 MB deps layer even when no dependency had changed. The api
# container imports src/ from WORKDIR (not from an installed dist), so the
# package itself does not need to be pip-installed here.
COPY requirements-backend.txt ./
# 0.1.2: install CPU-only torch FIRST so sentence-transformers doesn't pull
# the 2-4 GB of nvidia-cu* libs as transitive deps. M1 build was burning
# ~20 min downloading CUDA libraries that ARM64 can't use. Once torch is
# satisfied from the CPU index, the backend deps see it as already-installed
# and skip the GPU variant.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch torchvision

RUN pip install --no-cache-dir -r requirements-backend.txt

# 0.1.5: pre-install the spaCy English model into system site-packages while
# we're still root in the builder stage. presidio_analyzer loads this model
# the first time detect_pii() runs; without it pre-installed, presidio's
# auto-download falls back to `pip install --user` (because the runtime
# container is appuser, not root), the model lands in /home/appuser/.local/
# which spaCy's resolver doesn't search, and AnalyzerEngine() raises E050
# every single call. The singleton cache never gets populated (see 0.1.5
# guardrails_service fix), so every chat query repeats the model retry loop —
# ~130s of wasted wall time per query, with PII detection silently disabled.
#
# 0.3.1: swapped en_core_web_lg (425 MB) → en_core_web_md (33 MB) — saves
# ~392 MB uncompressed (~140 MB compressed). Measured against a 25-case
# financial-PERSON test corpus: md recall=1.000 = lg recall=1.000 (no missed
# PERSONs). Precision drops 4pp (one FP — "Q4" tagged as PERSON in a Tim
# Cook earnings-call sentence). For PII detection, recall is what matters
# (a miss is a privacy leak; an over-redaction is harmless), so the swap is
# safe. PHONE/EMAIL/SSN/CREDIT_CARD/IBAN/IP detection are regex/lib based
# (see guardrails_service.py:175-184), so they're untouched by the model
# size — only PERSON entity detection depends on spaCy NER.
RUN python -m spacy download en_core_web_md

# === Runtime stage ===
FROM python:3.12-slim

# 0.3.1: runtime stage no longer installs libxcb1 + libgl1 (they were here for
# docling's OpenCV; docling moved to the optional [docling] extra). Users who
# install docling via pip ".[docling]" need to add these system packages
# themselves (apt-get install libxcb1 libgl1 on Debian-based hosts).

# 0.2.0: silence the onnxruntime "Unknown CPU vendor" warning that fires on
# every import inside arm64-Linux-on-M1 containers. ORT_LOGGING_LEVEL=3
# means ERROR-only (default is 2 = WARNING). Test6 A/B falsified the
# hypothesis that this warning was a perf bottleneck; it's purely cosmetic
# log noise that surfaces on every script invocation including innocuous
# ones like `seed_qdrant.py --help`.
ENV ORT_LOGGING_LEVEL=3

# 0.2.2: suppress upstream deprecation warnings that fire from uvicorn's own
# websockets/protobuf imports BEFORE src/__init__.py runs the in-process
# filters. Applied at Python startup via PYTHONWARNINGS so it catches the
# import-machinery warnings that filterwarnings() can't.
ENV PYTHONWARNINGS="ignore:websockets.legacy is deprecated:DeprecationWarning,ignore:websockets.server.WebSocketServerProtocol is deprecated:DeprecationWarning,ignore:Type google.protobuf.pyext._message:DeprecationWarning"

WORKDIR /app

# Create non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ src/
COPY scripts/seed_qdrant.py scripts/seed_from_hf.py scripts/
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
#
# 0.2.3: same pattern for /app/logs and /app/cost_logs. 0.2.2 switched both
# from bind mounts to named volumes (api_logs, api_cost_logs) to fix the
# Linux UID issue — but only switching compose.minimal.yml is half the fix.
# The named volume inherits in-image ownership only if the directory exists
# in the image. We don't COPY logs/ or cost_logs/ (they're runtime artifacts),
# so the mount point was being created on the fly by docker as root:root.
# appuser PermissionError'd on the first event_log.attach_file_handler() →
# lifespan died → /v1/health never came up. 0.2.2 verify caught this; the
# fix was missed because I didn't grep the Dockerfile for the hf_cache
# pattern when writing the compose switch. (Fifth documented instance of
# "fixed one call site, missed the other" — see engineering-log.md.)
RUN mkdir -p /home/appuser/.cache/huggingface /app/logs /app/cost_logs && \
    chown -R appuser:appuser /home/appuser/.cache /app/logs /app/cost_logs
USER appuser

# Version- and commit-variant instructions are placed AFTER the heavy
# `COPY --from=builder site-packages` layer on purpose. LABEL version changes
# every release and GIT_SHA changes every commit; if they sit before the
# ~555 MB deps COPY, they invalidate its parent chain and force a new layer
# digest on every build, so `financebench upgrade` re-pulls the full deps layer
# even when no dependency changed. Keep them here, not at the top.
ARG GIT_SHA=unknown
# _git_sha() in src/api/main.py reads $GIT_SHA (the image has no .git/); the
# wizard passes --build-arg GIT_SHA=$(git rev-parse HEAD) from the host checkout.
ENV GIT_SHA=${GIT_SHA}
LABEL maintainer="Rishabh" \
      description="FinanceBench RAG Agent API" \
      version="0.3.4"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/v1/health')" || exit 1

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
