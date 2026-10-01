import { lazy, Suspense, useRef, useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { chartSpec } from "../chart";
import { formatCost } from "../format";
import { useLatest } from "../hooks";
import type { QueryResponse } from "../types";
import ResultTable from "./ResultTable";

// recharts is the heaviest dependency: load it only when a result actually has a chart.
const ResultChart = lazy(() => import("./ResultChart"));

const EXAMPLES = [
  "How many leads do we have in each stage?",
  "Top 5 industries by total budget of won deals",
  "New leads per month over the last year",
];

type State =
  | { status: "idle" }
  | { status: "loading"; question: string }
  | { status: "error"; error: ApiError }
  | { status: "done"; result: QueryResponse };

export default function QueryBar() {
  const [question, setQuestion] = useState("");
  const [state, setState] = useState<State>({ status: "idle" });
  const latest = useLatest();
  const inputRef = useRef<HTMLInputElement>(null);

  async function ask(q: string) {
    const text = q.trim();
    if (text.length < 3) return;
    const id = latest.next();
    setState({ status: "loading", question: text });
    try {
      const result = await api.query(text);
      if (latest.isCurrent(id)) setState({ status: "done", result });
    } catch (err) {
      if (!latest.isCurrent(id)) return;
      setState({ status: "error", error: err instanceof ApiError ? err : new ApiError(0, "Something went wrong") });
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    void ask(question);
  }

  function dismiss() {
    latest.next(); // discard any in-flight response
    setState({ status: "idle" });
  }

  return (
    <section className="querybar" aria-label="Ask a question about your pipeline">
      <form onSubmit={submit} className="query-form" role="search">
        <span className="spark" aria-hidden>✦</span>
        <input
          ref={inputRef}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask your pipeline anything — e.g. “which industries close the most revenue?”"
          maxLength={500}
          aria-label="Question"
        />
        <button className="primary" disabled={state.status === "loading" || question.trim().length < 3}>
          {state.status === "loading" ? "Thinking…" : "Ask"}
        </button>
      </form>

      {state.status === "idle" && (
        <div className="chips">
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              className="chip"
              onClick={() => {
                setQuestion(ex);
                void ask(ex);
              }}
            >
              {ex}
            </button>
          ))}
        </div>
      )}

      {state.status === "loading" && (
        <div className="card result" aria-live="polite" aria-busy="true">
          <p className="muted">Compiling “{state.question}” into a query…</p>
          <div className="skeleton line" />
          <div className="skeleton block" />
        </div>
      )}

      {state.status === "error" && <ErrorCard error={state.error} onDismiss={dismiss} />}
      {state.status === "done" && <Result result={state.result} onDismiss={dismiss} />}
    </section>
  );
}

function ErrorCard({ error, onDismiss }: { error: ApiError; onDismiss: () => void }) {
  const friendly: Record<string, string> = {
    unanswerable: "That doesn't look like something the CRM data can answer.",
    invalid_ir: "I couldn't turn that into a valid query. Try rephrasing with the names of fields or stages.",
    timeout: "That query was too slow and was cancelled after 5 seconds.",
    budget_exceeded: "The daily AI budget has been used up. Try again tomorrow.",
  };
  return (
    <div className="card result error-card" role="alert">
      <div className="result-head">
        <strong>{(error.code && friendly[error.code]) ?? error.message}</strong>
        <button className="ghost" onClick={onDismiss} aria-label="Dismiss">✕</button>
      </div>
      {error.code === "unanswerable" && <p className="muted">{error.message}</p>}
      {error.details.length > 0 && (
        <details>
          <summary>Why it was rejected</summary>
          <ul>{error.details.map((d) => <li key={d}>{d}</li>)}</ul>
        </details>
      )}
      {error.costUsd !== undefined && error.costUsd > 0 && <p className="meta">Cost {formatCost(error.costUsd)}</p>}
    </div>
  );
}

function Result({ result, onDismiss }: { result: QueryResponse; onDismiss: () => void }) {
  const spec = chartSpec(result);
  return (
    <div className="card result">
      <div className="result-head">
        <p className="explanation">
          <span className="badge">{result.explanation_source === "llm" ? "AI summary" : "Summary"}</span> {result.explanation}
        </p>
        <button className="ghost" onClick={onDismiss} aria-label="Dismiss results">✕</button>
      </div>
      {spec && (
        <Suspense fallback={<div className="skeleton block" />}>
          <ResultChart spec={spec} />
        </Suspense>
      )}
      <ResultTable columns={result.columns} rows={result.rows} />
      <div className="meta">
        {result.row_count} row{result.row_count === 1 ? "" : "s"}
        {result.truncated && " (limited to the first 100)"} · {Math.round(result.elapsed_ms)} ms · cost {formatCost(result.cost_usd)}
        {result.cached && " (cached)"}
      </div>
      <details className="sql">
        <summary>Show generated SQL</summary>
        <pre>{result.sql}</pre>
        <p className="meta">Built deterministically from a validated query description; read-only, 5 s timeout.</p>
      </details>
    </div>
  );
}
