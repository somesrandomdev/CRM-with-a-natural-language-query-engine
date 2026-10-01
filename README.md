# Clearpipe

An AI-native CRM. Its flagship feature is a natural-language query bar that **compiles** English
questions into validated, read-only SQL. The LLM is a query-compiler frontend, not a chatbot:
it only ever produces a typed intermediate representation (IR); all SQL is generated and checked
by deterministic code.

> Build order is visible in the commit history; each milestone is one commit.

## Quick start

```bash
cp .env.example .env            # set JWT_SECRET
make install                    # Python 3.12 venv via uv
docker compose up -d db         # or any Postgres 16 matching DATABASE_URL
make migrate seed               # schema + 500 demo leads
make api                        # http://localhost:8000/docs
```

Demo logins (seed data only): `admin@clearpipe.dev` and `maya.chen@clearpipe.dev` (rep), password
`clearpipe-demo`.

`docker compose up --build` runs the API and database together.

## Access model

| Role  | Leads                    | Users admin |
|-------|--------------------------|-------------|
| admin | all                      | yes         |
| rep   | only leads they own      | no          |

Authorization is enforced with FastAPI dependencies (`app/deps.py`); a rep requesting someone
else's lead gets a 404 so ids cannot be probed. Roles are read from the database on every request,
never trusted from the token.

## Tests

`make test` runs against a real PostgreSQL (`TEST_DATABASE_URL`, default
`clearpipe_test` on localhost). The schema is built by running the actual Alembic migrations, and a
test asserts the ORM models and migrations have not drifted.

## The query compiler (`nlquery/`)

`POST /query {"question": "..."}` compiles English into a validated, read-only SQL query:

```
question ──► compiler.py ──► QueryIR ──► check_ir ──► builder.py ──► validator.py ──► executor.py ──► explainer.py
             (LLM, forced   (typed,     (vs. live    (sqlglot AST,  (AST walk,       (savepoint,     (LLM, 1-2
              tool call)     no SQL)     catalog)     no strings)    allow-lists)     5s timeout,     sentences)
                                                                                      read-only)
```

The LLM appears twice: at the IR boundary and in the final summary. Everything in between is
ordinary, tested code.

* **IR, not SQL** (`ir.py`). The model must answer through one forced tool call whose schema is
  `QueryIR` (`tables, select, filters, joins, group_by, order_by, limit, chart_hint`). There is no
  field that can hold SQL; unknown fields are rejected. `select` is an addition to the brief's field
  list: without it an IR cannot express aggregates. A question the schema cannot answer comes back
  as an `unanswerable_reason`, not a guessed query.
* **Live schema in the prompt** (`catalog.py`, `prompt.md`). Tables, columns, enum labels, sample
  values for low-cardinality text columns, foreign keys and row counts are introspected on every
  request. Exposure is an explicit allow-list (`users.hashed_password` does not exist as far as the
  compiler is concerned). The schema hash covers structure only, not row counts, so inserts do not
  invalidate cached compilations.
* **IR validation with repair** (`ir.check_ir`). Tables, columns, enum values, joins (must follow a
  foreign key and form a tree), operator/type compatibility, and group-by consistency are checked
  against the catalog. Failures are fed back to the model for one repair attempt, then the request
  fails with the validator's messages.
* **Deterministic builder** (`builder.py`). IR -> sqlglot AST -> SQL. Values become escaped literals
  (LIKE wildcards escaped, hostile strings stay literals), relative dates compile to
  `CURRENT_TIMESTAMP - INTERVAL` / `DATE_TRUNC` expressions, so cached IRs never go stale.
  Reps are restricted to their own leads (and those leads' activities) by CTEs the builder
  injects *after* compilation, so one cached IR serves every user.
* **Validator** (`validator.py`). Parses the final SQL with sqlglot and enforces, on the AST:
  exactly one statement; SELECT only, with DML/DDL rejected anywhere in the tree (including inside
  CTEs); an allow-list of node types and functions (no `pg_sleep`, `pg_read_file`, `set_config`,
  ...); table and column allow-lists (columns are resolved against the exposed schema, `*` is only
  legal in `COUNT(*)`); and a literal `LIMIT`, injected as 100 when absent and clamped when larger.
  The executed SQL is regenerated from the validated AST, with comments stripped.
* **Executor** (`executor.py`). Runs in a savepoint with `statement_timeout = 5s` and the
  transaction forced read-only (defense in depth: a bypassed validator still cannot write).
* **Explainer** (`explainer.py`). One `claude-sonnet-4-6` call over the IR and a 10-row sample;
  falls back to a deterministic description if the call fails or misbehaves.

Not covered (v1): OR across different columns, HAVING, self-joins, set operations, window functions.

## Note ingestion (`pipeline/`)

`POST /ingest/note` takes raw call/email text for a lead and returns `202` immediately. A background
worker extracts `{budget_hint, timeline, objections[], sentiment}` with `claude-haiku-4-5` (forced
tool call, validated against a pydantic model) into a **pending proposal**; nothing touches the lead
until a human reviews it.

* **Idempotency.** `Idempotency-Key` is required. Same key + same payload returns the original job
  (`200`); same key + different payload is a `422`. Keys are per user and enforced by a unique
  constraint, so a concurrent duplicate loses the race cleanly.
* **Queue.** `ingest_jobs` is the queue: `FOR UPDATE SKIP LOCKED` claims with a lease, so several
  workers can run and a crashed worker's job is picked up again when its lease expires. The LLM call
  holds no transaction or row lock.
* **Retries and DLQ.** Failures (LLM errors, invalid extraction output) retry with exponential
  backoff; after 3 attempts the job moves to `ingest_dead_letters` with the payload and error.
  Admins can list it (`GET /ingest/dead-letters`) and requeue (`POST .../{id}/retry`). A missing API
  key is treated as configuration, not a job failure: jobs stay queued and burn no attempts.
* **Review.** `GET /ingest/proposals` returns each proposal with a field-by-field diff (current vs.
  proposed). `PATCH /ingest/proposals/{id}` with `{"action": "accept" | "reject"}` applies or
  discards it; `accept` can take a `fields` subset. Deciding is idempotent for the same action and a
  `409` for the opposite, and accepting also logs the note as an activity on the lead.

The worker runs inside the API process by default (`WORKER_ENABLED=false` to disable) or standalone:
`python -m pipeline.worker`.

## Lead scoring (`scoring/`)

`GET /leads/{id}/score` returns `{score, breakdown, rationale}`. The **score is deterministic**:
`scoring/rules.py` defines explicit rules (the checked-in ones are placeholders for you to replace;
the contract is documented at the top of the file), `scoring/scoring.py` applies them, and the
tests enforce that weights sum to 100 and no rule ever overshoots its maximum. The LLM
(`claude-haiku-4-5`) only writes the one- or two-sentence `rationale`, and its text is discarded
in favour of a deterministic sentence if it cites any number that is not in the score data, or if
the call fails. The score never depends on the model.

## Frontend (`frontend/`)

React 18 + Vite + TypeScript, with recharts for charts.

```bash
make web-install && make web      # http://localhost:5173 (API on :8000)
make web-test                     # unit tests + type-checked production build
```

* **Pipeline**: a kanban board with one column per stage; drag a card (or use its "move" select, for
  keyboard users) to change stage. Updates are optimistic and roll back with a message if the API
  refuses.
* **Leads**: server-side search (debounced), stage filter, and pagination. Stale responses from
  superseded searches are discarded.
* **Query bar**: input -> loading state -> AI summary, chart, results table, row count / latency /
  `cost_usd`, and a "show generated SQL" disclosure. The model's `chart_hint` is only a suggestion:
  `chart.ts` falls back to the table alone when the data's shape can't support it. Refusals and
  validation failures get a friendly message plus the validator's details.
* **Lead drawer**: details, the deterministic score breakdown with its rationale, and a form to submit
  a call/email note (one idempotency key per draft).
* **Ingest**: pending proposals with a field-by-field current -> proposed diff, per-field checkboxes
  for partial accepts, accept/reject, history tabs, and (for admins) the dead-letter queue with retry.

The JWT is kept in `localStorage` for simplicity; a production deployment would prefer an
httpOnly cookie.

## Cost controls (`llm/`)

Every model call in the codebase goes through one wrapper, `llm.LLMClient`. Four hard rules, each
covered by tests:

1. **One door.** Only `llm/client.py` imports the Anthropic SDK (a test scans the source tree).
2. **No Opus.** Models must be on an allow-list (`claude-sonnet-4-6`, `claude-haiku-4-5`), and any
   model id containing "opus" is refused even if someone adds it to the list. Refusal happens before
   the cache, the budget check, or the network. A test also fails if an Opus id appears anywhere in
   the source.
3. **Every call is logged** to `costs.jsonl` as `{ts, task, model, input_tokens, output_tokens, usd,
   cached, saved_usd, prompt_version}`. Prompts and responses are never written. Set
   `LLM_DAILY_BUDGET_USD` for a hard daily cap (calls then fail with `429 budget_exceeded`; queued
   ingest jobs wait instead of burning retries).
4. **Responses are cached** under `sha256(schema_hash, prompt_version, input)` (input = task, model
   and user message). A schema change or prompt-version bump misses automatically; row counts are
   deliberately not part of the schema hash. Invalid answers (an IR that fails validation, an
   extraction that fails parsing, a rationale that cites invented numbers) are never cached, so a bad
   answer is regenerated rather than replayed. Cache hits are free and logged as such.

`POST /query` returns `cost_usd` (actual spend for that request, `0` when fully cached) and
`cached` on every response, including errors. `tests/test_prompt_lock.py` fails if a prompt is
edited without a version bump, which is what keeps the cache key honest.
