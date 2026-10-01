# Query evals

`query_golden.jsonl` holds labelled cases, one JSON object per line:

```json
{"id": "leads-per-stage", "question": "How many leads do we have in each stage?",
 "expected_ir": { ...QueryIR... },
 "expected_rows": [["won", 93], ["lost", 92]],
 "ordered": true,
 "expect_unanswerable": false,
 "tags": ["aggregate"], "notes": "free text"}
```

Provide at least one of `expected_ir`, `expected_rows`, or `expect_unanswerable: true`.

| Field | Meaning |
|---|---|
| `expected_ir` | The IR a correct compiler produces. Enables **exact match** (after canonicalisation: aliases, filter/join order and `limit: null` vs `100` are ignored). If `expected_rows` is absent, the expected rows are derived by executing this IR through the production builder/validator/executor as admin. |
| `expected_rows` | Authoritative rows for **result equivalence** (list of lists, select order). Compared as a multiset with float tolerance; a column-permuted result also matches. |
| `ordered` | Compare row order too. Defaults to `true` when `expected_ir` has an `order_by`. |
| `expect_unanswerable` | The API should refuse (`422 unanswerable`). |

Labels are validated: an `expected_ir` that does not check against the live schema is reported as
`BAD` (a labelling bug), not as a model failure.

The nine cases checked in are starters to show the format; replace/extend them with your own.
Labelling with `expected_ir` only is robust to reseeding; `expected_rows` pins the data, so reseed
with `make eval-seed` (fixed seed and `--as-of`) before running them.

## Running

```bash
make eval-stub   # no API key, no cost: the model is replaced by the labelled IRs (see below)
make eval        # real model against a running API; costs money
```

`make eval` needs the API running with `ANTHROPIC_API_KEY` set and `DATABASE_URL` pointing at the
same database. Options: `--ids a b`, `--tag join`, `--min-exact 0.8 --min-equivalence 0.9`
(non-zero exit below the thresholds). Results are written to `evals/results/` (git-ignored).

Cases labelled only with `expected_rows` are skipped in the stubbed run (there is no IR to replay);
they are scored by `make eval`.

**What the stubbed run proves, and what it does not.** In `eval-stub` mode the API runs with
`LLM_BACKEND=replay`, which answers each question with that case's own `expected_ir`. Exact match is
therefore 100% by construction. The run exercises everything *after* the model: IR checks, builder,
validator, executor, auth, HTTP, and the eval harness itself. Model quality is only measured by
`make eval`.
