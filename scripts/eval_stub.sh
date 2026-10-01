#!/usr/bin/env bash
# Run the golden eval against a throwaway API instance whose LLM is replaced by the labelled IRs.
# Requires a migrated database (DATABASE_URL). Seeds it with a fixed seed and as-of date.
set -euo pipefail

PY="${PY:-.venv/bin/python}"
PORT="${EVAL_PORT:-8765}"

"$PY" -m seed --reset --seed 42 --as-of 2026-06-30 >/dev/null

LLM_BACKEND=replay "$PY" -m uvicorn app.main:app --port "$PORT" --log-level warning &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if curl -fs "http://localhost:$PORT/health" >/dev/null; then break; fi
  sleep 0.2
done

API_URL="http://localhost:$PORT" "$PY" -m evals.eval_queries --min-exact 1.0 --min-equivalence 1.0 "$@"
