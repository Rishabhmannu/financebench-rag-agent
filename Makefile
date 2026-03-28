.PHONY: run dev frontend test test-unit test-integration eval lint ingest seed-db docker-up docker-down clean

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

# --- Cleanup ---
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache/ htmlcov/ .coverage
