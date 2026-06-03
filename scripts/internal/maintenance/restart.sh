#!/usr/bin/env bash
# Restart script for Enterprise RAG Agent
# Starts infrastructure, restores state from persisted volumes, and launches API + frontend.
#
# Usage:
#   ./scripts/restart.sh              # Start everything (infra + API + frontend)
#   ./scripts/restart.sh --infra-only # Only start Qdrant + PostgreSQL

set -euo pipefail

INFRA_ONLY=false
if [ "${1:-}" = "--infra-only" ]; then
    INFRA_ONLY=true
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

echo "=== Enterprise RAG Agent — Restart ==="
echo "Project dir: $PROJECT_DIR"
echo ""

# 0. Pre-flight checks
if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found. Copy .env.example and fill in your API keys:"
    echo "  cp .env.example .env"
    exit 1
fi

# 1. Start infrastructure (Qdrant + PostgreSQL)
echo "[1/5] Starting infrastructure (Qdrant + PostgreSQL)..."
docker compose up -d qdrant postgres
echo "  Waiting for services to become healthy..."

TRIES=0
MAX_TRIES=30
while [ $TRIES -lt $MAX_TRIES ]; do
    QDRANT_HEALTHY=$(docker compose ps qdrant --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health',''))" 2>/dev/null || echo "")
    PG_HEALTHY=$(docker compose ps postgres --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health',''))" 2>/dev/null || echo "")

    if [[ "$QDRANT_HEALTHY" == *"healthy"* ]] && [[ "$PG_HEALTHY" == *"healthy"* ]]; then
        echo "  Qdrant: healthy"
        echo "  PostgreSQL: healthy"
        break
    fi

    TRIES=$((TRIES + 1))
    if [ $TRIES -eq $MAX_TRIES ]; then
        echo "  WARNING: Timed out waiting for health checks."
        echo "  Verifying Qdrant directly via HTTP..."
        if curl -s -f http://localhost:6333/healthz >/dev/null 2>&1; then
            echo "  Qdrant responds OK (ignore container healthcheck status)"
        else
            echo "  Qdrant is not responding on http://localhost:6333"
            docker compose ps
            exit 1
        fi
        break
    fi
    sleep 1
done

# 2. Check Qdrant collection
echo ""
echo "[2/5] Checking Qdrant collection..."
QDRANT_HOST="${QDRANT_HOST:-localhost}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
COLLECTION_STATUS=$(curl -s "http://${QDRANT_HOST}:${QDRANT_PORT}/collections/financial_docs" 2>/dev/null || echo "")

if echo "$COLLECTION_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['result']['points_count']>0" 2>/dev/null; then
    POINTS=$(echo "$COLLECTION_STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['points_count'])")
    echo "  Collection 'financial_docs' found with $POINTS points — state restored"
else
    echo "  Collection empty or missing. Re-seeding with sample data..."
    python3 scripts/seed_qdrant.py --sample
    echo "  Seeding complete"
fi

# 3. Check PostgreSQL checkpointer tables
echo ""
echo "[3/5] Checking PostgreSQL checkpointer..."
PG_USER="${POSTGRES_USER:-rag_user}"
PG_DB="${POSTGRES_DB:-rag_agent}"
TABLE_CHECK=$(docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -c "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE 'checkpoint%';" -t 2>/dev/null | tr -d ' ' || echo "0")

if [ "${TABLE_CHECK:-0}" -gt "0" ] 2>/dev/null; then
    echo "  Checkpointer tables found — HITL state preserved from previous session"
else
    echo "  No checkpointer tables yet (will be created on first API startup)"
fi

if [ "$INFRA_ONLY" = true ]; then
    echo ""
    echo "=== Infrastructure ready (--infra-only mode) ==="
    echo ""
    echo "Start the API and frontend manually with:"
    echo "  make run         # API server on http://localhost:8000"
    echo "  make frontend    # Gradio UI on http://localhost:7860"
    exit 0
fi

# 4. Stop any existing API / frontend processes to avoid port conflicts
echo ""
echo "[4/5] Starting API server..."
pkill -f "uvicorn src.api.main:app" 2>/dev/null || true
sleep 1

nohup uvicorn src.api.main:app --port 8000 > "$LOG_DIR/api.log" 2>&1 &
API_PID=$!
echo "  API PID: $API_PID (logs: $LOG_DIR/api.log)"

# Wait for API to come up
TRIES=0
MAX_TRIES=30
while [ $TRIES -lt $MAX_TRIES ]; do
    if curl -s -f http://localhost:8000/health >/dev/null 2>&1; then
        echo "  API is healthy: http://localhost:8000"
        break
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo "  ERROR: API process died. Last 20 log lines:"
        tail -20 "$LOG_DIR/api.log"
        exit 1
    fi
    TRIES=$((TRIES + 1))
    sleep 1
done
if [ $TRIES -eq $MAX_TRIES ]; then
    echo "  ERROR: API did not respond within 30 seconds. Check $LOG_DIR/api.log"
    exit 1
fi

# 5. Start Gradio frontend
echo ""
echo "[5/5] Starting Gradio frontend..."
pkill -f "src.frontend.gradio_app" 2>/dev/null || true
sleep 1

nohup python -m src.frontend.gradio_app > "$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "  Frontend PID: $FRONTEND_PID (logs: $LOG_DIR/frontend.log)"

# Wait for frontend
TRIES=0
MAX_TRIES=20
while [ $TRIES -lt $MAX_TRIES ]; do
    if curl -s -f http://localhost:7860/ >/dev/null 2>&1; then
        echo "  Frontend is up: http://localhost:7860"
        break
    fi
    if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "  ERROR: Frontend process died. Last 20 log lines:"
        tail -20 "$LOG_DIR/frontend.log"
        exit 1
    fi
    TRIES=$((TRIES + 1))
    sleep 1
done

# Save PIDs so shutdown.sh can kill them cleanly
echo "$API_PID" > "$LOG_DIR/.api.pid"
echo "$FRONTEND_PID" > "$LOG_DIR/.frontend.pid"

echo ""
echo "=== All services running ==="
echo ""
echo "  API:       http://localhost:8000  (docs: /docs)"
echo "  Frontend:  http://localhost:7860"
echo "  Qdrant:    http://localhost:6333/dashboard"
echo ""
echo "Logs:"
echo "  tail -f $LOG_DIR/api.log"
echo "  tail -f $LOG_DIR/frontend.log"
echo ""
echo "To stop: ./scripts/shutdown.sh"
