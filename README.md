# Clearpipe

An AI-native CRM. Its flagship feature is a natural-language query bar that **compiles** English
questions into validated, read-only SQL. The LLM is a query-compiler frontend, not a chatbot:
it only ever produces a typed intermediate representation (IR); all SQL is generated and checked
by deterministic code.

> Status: milestone 1 (backend skeleton). See the commit history for the build order.

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
