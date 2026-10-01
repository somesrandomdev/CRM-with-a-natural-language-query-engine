"""Run the golden NL->query cases against a live Clearpipe API and report quality.

    python -m evals.eval_queries --api-url http://localhost:8000 [--min-exact 0.8]

For each case the runner POSTs the question to `/query` and compares the response with the label:

* exact match  - the returned IR equals `expected_ir` after canonicalisation (aliases, filter/join
                 order and `limit: null` vs 100 are ignored; see `nlquery/canonical.py`);
* equivalence  - the returned rows equal the expected rows (`expected_rows`, or the rows obtained by
                 executing `expected_ir` through the same builder/validator/executor, as admin).
                 A different IR that returns the same answer counts as equivalent but not exact.

`expected_ir` labels are executed directly against the database named by DATABASE_URL, so run this
against the same database the API serves. Questions labelled `expect_unanswerable` pass when the API
refuses them with the `unanswerable` code.

Costs real money with the Anthropic backend. With the API started in `LLM_BACKEND=replay` mode the
model is replaced by the labelled IRs themselves, so exact-match is trivially 100% and the run only
validates the pipeline downstream of the model (see `make eval-stub`).
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.db import get_sessionmaker
from evals.compare import rows_equivalent
from evals.golden import GoldenCase, load_golden
from nlquery.builder import build_sql
from nlquery.canonical import canonicalize
from nlquery.catalog import load_catalog
from nlquery.executor import execute_readonly
from nlquery.ir import IRValidationError, QueryIR, check_ir, parse_ir
from nlquery.service import exposed_schema
from nlquery.validator import validate_sql

DEFAULT_GOLDEN = Path(__file__).parent / "query_golden.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"


class GoldenError(Exception):
    """A label is itself invalid (a labelling bug, not a model failure)."""


@dataclass
class CaseResult:
    id: str
    question: str
    status: str  # pass | fail | error | golden_invalid
    exact_match: bool | None = None
    equivalent: bool | None = None
    chart_hint_match: bool | None = None
    unanswerable_ok: bool | None = None
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    got_ir: dict[str, Any] | None = None
    got_sql: str | None = None
    expected_rows: list[list[Any]] | None = None
    got_rows: list[list[Any]] | None = None


class ExpectedRows:
    """Derives expected rows by running a labelled IR through the production pipeline (as admin)."""

    def __init__(self, session: Session | None = None) -> None:
        self._owns_session = session is None
        self._session = session or get_sessionmaker()()
        self._catalog = load_catalog(self._session)

    def rows_for(self, ir: QueryIR) -> list[list[Any]]:
        errors = check_ir(ir, self._catalog)
        if errors:
            raise GoldenError(
                "expected_ir is invalid against the live schema: " + "; ".join(errors)
            )
        validated = validate_sql(build_sql(ir, self._catalog), exposed_schema(self._catalog))
        return execute_readonly(self._session, validated).rows

    def close(self) -> None:
        if self._owns_session:
            self._session.close()


def evaluate_case(case: GoldenCase, client: httpx.Client, expected: ExpectedRows) -> CaseResult:
    result = CaseResult(case.id, case.question, status="pass")
    try:
        resp = client.post("/query", json={"question": case.question})
    except httpx.HTTPError as exc:
        result.status, result.notes = "error", [f"request failed: {exc}"]
        return result
    result.latency_ms = round(resp.elapsed.total_seconds() * 1000, 1)
    try:
        body = resp.json()
    except json.JSONDecodeError:
        result.status, result.notes = "error", [f"non-JSON response (HTTP {resp.status_code})"]
        return result
    result.cost_usd = float(body.get("cost_usd") or 0.0)

    if case.expect_unanswerable:
        code = body.get("error", {}).get("code")
        result.unanswerable_ok = resp.status_code == 422 and code == "unanswerable"
        result.status = "pass" if result.unanswerable_ok else "fail"
        if not result.unanswerable_ok:
            result.notes.append(
                f"expected a refusal, got HTTP {resp.status_code} ({code or 'answer'})"
            )
        return result

    if resp.status_code != 200:
        err = body.get("error", {})
        result.status = "fail"
        result.notes.append(f"HTTP {resp.status_code}: {err.get('code')} {err.get('message', '')}")
        result.notes.extend(err.get("details", [])[:3])
        result.exact_match = False if case.expected_ir else None
        result.equivalent = False
        return result

    result.got_ir, result.got_sql, result.got_rows = body["ir"], body["sql"], body["rows"]

    try:
        expected_rows = case.expected_rows
        if expected_rows is None:
            assert case.expected_ir is not None
            expected_rows = expected.rows_for(case.expected_ir)
    except GoldenError as exc:
        result.status, result.notes = "golden_invalid", [str(exc)]
        return result
    result.expected_rows = expected_rows

    if case.expected_ir is not None:
        try:
            got_ir = parse_ir(body["ir"])
        except IRValidationError as exc:  # the API validated this already; defensive
            result.status, result.notes = "error", [f"API returned an unparseable IR: {exc}"]
            return result
        result.exact_match = canonicalize(got_ir) == canonicalize(case.expected_ir)
        result.chart_hint_match = got_ir.chart_hint == case.expected_ir.chart_hint
    ordered = case.ordered
    if ordered is None:
        ordered = bool(case.expected_ir and case.expected_ir.order_by)
    result.equivalent = rows_equivalent(result.got_rows, expected_rows, ordered=ordered)
    result.status = "pass" if result.equivalent else "fail"
    if result.equivalent and result.exact_match is False:
        result.notes.append("different IR, same answer")
    if not result.equivalent:
        result.notes.append(
            f"rows differ: got {len(result.got_rows)}, expected {len(expected_rows)}"
        )
    return result


@dataclass(frozen=True)
class Summary:
    total: int
    passed: int
    failed: int
    errors: int
    golden_invalid: int
    exact_rate: float | None
    exact_n: int
    equivalence_rate: float | None
    equivalence_n: int
    chart_hint_rate: float | None
    unanswerable_rate: float | None
    unanswerable_n: int
    total_cost_usd: float
    avg_latency_ms: float


def _rate(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 4) if flags else None


def summarize(results: list[CaseResult]) -> Summary:
    scored = [r for r in results if r.status not in ("golden_invalid", "error")]
    exact = [bool(r.exact_match) for r in scored if r.exact_match is not None]
    equiv = [bool(r.equivalent) for r in scored if r.equivalent is not None]
    charts = [bool(r.chart_hint_match) for r in scored if r.chart_hint_match is not None]
    unans = [bool(r.unanswerable_ok) for r in scored if r.unanswerable_ok is not None]
    return Summary(
        total=len(results),
        passed=sum(r.status == "pass" for r in results),
        failed=sum(r.status == "fail" for r in results),
        errors=sum(r.status == "error" for r in results),
        golden_invalid=sum(r.status == "golden_invalid" for r in results),
        exact_rate=_rate(exact),
        exact_n=len(exact),
        equivalence_rate=_rate(equiv),
        equivalence_n=len(equiv),
        chart_hint_rate=_rate(charts),
        unanswerable_rate=_rate(unans),
        unanswerable_n=len(unans),
        total_cost_usd=round(sum(r.cost_usd for r in results), 6),
        avg_latency_ms=round(sum(r.latency_ms for r in results) / len(results), 1)
        if results
        else 0.0,
    )


def run_eval(
    cases: list[GoldenCase], client: httpx.Client, expected: ExpectedRows
) -> list[CaseResult]:
    # Sequential on purpose: `expected` owns a DB session, and the eval is cost/rate-limit bound.
    return [evaluate_case(c, client, expected) for c in cases]


def _pct(rate: float | None, n: int) -> str:
    return "n/a" if rate is None else f"{rate * 100:5.1f}%  ({round(rate * n)}/{n})"


def format_report(results: list[CaseResult], summary: Summary) -> str:
    lines = [f"Clearpipe query eval: {summary.total} cases", "-" * 60]
    lines.append(f"exact match        {_pct(summary.exact_rate, summary.exact_n)}")
    lines.append(f"result equivalence {_pct(summary.equivalence_rate, summary.equivalence_n)}")
    if summary.chart_hint_rate is not None:
        lines.append(f"chart hint         {_pct(summary.chart_hint_rate, summary.exact_n)}")
    if summary.unanswerable_n:
        lines.append(
            f"refusals correct   {_pct(summary.unanswerable_rate, summary.unanswerable_n)}"
        )
    lines.append(
        f"passed {summary.passed}  failed {summary.failed}  errors {summary.errors}  "
        f"invalid labels {summary.golden_invalid}"
    )
    lines.append(
        f"cost ${summary.total_cost_usd:.4f}   avg latency {summary.avg_latency_ms:.0f} ms"
    )
    problems = [r for r in results if r.status != "pass" or r.exact_match is False]
    if problems:
        lines += ["", "Cases needing attention:"]
        for r in problems:
            tag = {"pass": "~", "fail": "FAIL", "error": "ERR ", "golden_invalid": "BAD "}[r.status]
            lines.append(f"  [{tag}] {r.id}: {r.question}")
            lines.extend(f"         {note}" for note in r.notes)
    return "\n".join(lines)


def login(client: httpx.Client, email: str, password: str) -> None:
    resp = client.post("/auth/login", data={"username": email, "password": password})
    if resp.status_code != 200:
        raise SystemExit(f"login failed for {email} (HTTP {resp.status_code})")
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--api-url", default=os.environ.get("API_URL", "http://localhost:8000"))
    p.add_argument("--email", default=os.environ.get("EVAL_EMAIL", "admin@clearpipe.dev"))
    p.add_argument("--password", default=os.environ.get("EVAL_PASSWORD", "clearpipe-demo"))
    p.add_argument("--ids", nargs="*", help="only run these case ids")
    p.add_argument("--tag", help="only run cases with this tag")
    p.add_argument("--min-exact", type=float, help="fail (exit 1) below this exact-match rate")
    p.add_argument(
        "--min-equivalence", type=float, help="fail (exit 1) below this equivalence rate"
    )
    p.add_argument("--no-save", action="store_true", help="do not write evals/results/*.json")
    args = p.parse_args(argv)

    golden = load_golden(args.golden)
    for err in golden.errors:
        print(f"golden file error: {err}", file=sys.stderr)
    if golden.errors:
        return 2
    cases = [
        c for c in golden.cases
        if (not args.ids or c.id in args.ids) and (not args.tag or args.tag in c.tags)
    ]  # fmt: skip
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    expected = ExpectedRows()
    try:
        with httpx.Client(base_url=args.api_url, timeout=90.0) as client:
            login(client, args.email, args.password)
            results = run_eval(cases, client, expected)
    finally:
        expected.close()

    summary = summarize(results)
    print(format_report(results, summary))
    if not args.no_save:
        RESULTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        payload = {"summary": asdict(summary), "cases": [asdict(r) for r in results]}
        for name in (f"{stamp}.json", "latest.json"):
            (RESULTS_DIR / name).write_text(json.dumps(payload, indent=2, default=str))

    failed = summary.golden_invalid > 0 or summary.errors > 0
    if args.min_exact is not None and (summary.exact_rate or 0.0) < args.min_exact:
        failed = True
    if (
        args.min_equivalence is not None
        and (summary.equivalence_rate or 0.0) < args.min_equivalence
    ):
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
