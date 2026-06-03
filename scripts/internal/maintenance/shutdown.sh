#!/usr/bin/env bash
# Safe shutdown script for Enterprise RAG Agent
# Gracefully stops all services and preserves state.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/logs"

echo "=== Enterprise RAG Agent — Safe Shutdown ==="
echo "Project dir: $PROJECT_DIR"
echo ""

# 1. Stop local Python processes — prefer saved PIDs, fall back to pkill
echo "[1/3] Stopping local Python processes (API, frontend)..."

stop_pid_file() {
    local pid_file="$1"
    local label="$2"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && echo "  Stopped $label (PID $pid)" || true
            # Give it up to 5s to exit cleanly, then SIGKILL
            for _ in 1 2 3 4 5; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
    fi
}

stop_pid_file "$LOG_DIR/.api.pid" "API"
stop_pid_file "$LOG_DIR/.frontend.pid" "frontend"

# Fallback: catch anything started outside of restart.sh
pkill -f "uvicorn src.api.main:app" 2>/dev/null && echo "  Killed stray uvicorn process" || true
pkill -f "src.frontend.gradio_app" 2>/dev/null && echo "  Killed stray gradio process" || true

sleep 1

# 2. Stop Docker Compose services (preserves volumes)
echo ""
echo "[2/3] Stopping Docker Compose services (preserving volumes)..."
docker compose stop 2>/dev/null && echo "  All containers stopped" || echo "  No containers to stop"

# 3. Summary
echo ""
echo "[3/3] Verifying..."
echo ""
echo "Docker containers:"
docker compose ps 2>/dev/null || docker ps --filter "name=campusx-langgraph" --format "table {{.Names}}\t{{.Status}}" 2>/dev/null || echo "  None"

echo ""
echo "Local processes on ports 8000 and 7860:"
lsof -iTCP:8000 -sTCP:LISTEN 2>/dev/null || echo "  Port 8000: free"
lsof -iTCP:7860 -sTCP:LISTEN 2>/dev/null || echo "  Port 7860: free"

echo ""
echo "=== Shutdown complete ==="
echo "Volumes preserved: qdrant_data, pg_data (your data and checkpoints are safe)"
echo "To fully remove containers (but keep volumes): docker compose down"
echo "To restart: ./scripts/restart.sh"
