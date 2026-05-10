.PHONY: run dev frontend test test-unit test-integration eval lint format ingest seed-db jwt docker-up docker-down docker-all docker-build docker-prod docker-logs docker-ps docker-restart check clean migrate migrate-down migrate-create migrate-current

# --- Development ---
run:
	uvicorn src.api.main:app --reload --port 8000

dev: docker-up
	uvicorn src.api.main:app --reload --port 8000

frontend:
	python -m src.frontend.gradio_app

# --- Testing ---
test:
	pytest tests/unit/ tests/integration/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v --timeout=120

eval:
	python tests/evaluation/run_evaluation.py --output tests/evaluation/eval_results/latest.json

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	ruff check --fix src/ tests/
	ruff format src/ tests/

# --- Data ---
ingest:
	python scripts/ingest_documents.py --input data/raw/ --collection financial_docs

seed-db:
	python scripts/seed_qdrant.py --sample

jwt:
	python scripts/generate_jwt.py --role finance --user-id test_user

# --- Docker ---
docker-up:
	docker compose up -d qdrant postgres

docker-down:
	docker compose down

docker-all:
	docker compose up --build

# --- Production ---
docker-build:
	docker compose build

docker-prod:
	docker compose up -d --build

docker-logs:
	docker compose logs -f

docker-ps:
	docker compose ps

docker-restart:
	docker compose restart api frontend

# --- Migrations (Sprint 9.0: alembic for the roles table; will grow) ---
migrate:
	alembic upgrade head

migrate-down:
	alembic downgrade -1

migrate-current:
	alembic current

# Usage: make migrate-create m="add foo table"
migrate-create:
	alembic revision -m "$(m)"

# --- Checks ---
check: lint test-unit
	@echo "All checks passed"

# --- Cleanup ---
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ htmlcov/ .coverage
