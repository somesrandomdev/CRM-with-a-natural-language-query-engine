PY := .venv/bin/python
BIN := .venv/bin

.PHONY: web-install web web-test web-build install up down migrate seed eval-seed eval eval-stub test lint fmt typecheck api

install:
	uv venv --python 3.12 .venv
	uv pip install -p $(PY) -e ".[dev]"

up:
	docker compose up --build

down:
	docker compose down

migrate:
	$(BIN)/alembic upgrade head

seed:
	$(PY) -m seed --reset

# Reproducible data for labelled evals (pinned seed and 'now').
eval-seed:
	$(PY) -m seed --reset --seed 42 --as-of 2026-06-30

# Real model, real cost. Needs the API running (with ANTHROPIC_API_KEY) on API_URL.
eval:
	$(PY) -m evals.eval_queries $(EVAL_ARGS)

# No API key, no cost: the model is replaced by the labelled IRs. Validates everything downstream.
eval-stub:
	PY=$(PY) scripts/eval_stub.sh

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy app nlquery llm seed evals

api:
	$(BIN)/uvicorn app.main:app --reload

web-install:
	cd frontend && npm ci

web:
	cd frontend && npm run dev

web-test:
	cd frontend && npm test && npm run build
