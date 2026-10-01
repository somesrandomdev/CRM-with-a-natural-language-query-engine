PY := .venv/bin/python
BIN := .venv/bin

.PHONY: install up down migrate seed test lint fmt typecheck api

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

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy app

api:
	$(BIN)/uvicorn app.main:app --reload
