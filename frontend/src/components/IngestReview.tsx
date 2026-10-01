import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { formatCost, titleCase } from "../format";
import type { DeadLetter, FieldChange, Proposal, ProposalStatus } from "../types";
import { useAuth } from "../auth";

const STATUSES: ProposalStatus[] = ["pending", "accepted", "rejected"];

function show(field: string, value: unknown): string {
  if (value === null || value === undefined || (Array.isArray(value) && value.length === 0)) return "—";
  if (field === "budget_usd" && typeof value === "number") return `$${value.toLocaleString("en-US")}`;
  if (Array.isArray(value)) return value.join("; ");
  return String(value);
}

export default function IngestReview() {
  const { user } = useAuth();
  const [status, setStatus] = useState<ProposalStatus>("pending");
  const [items, setItems] = useState<Proposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const page = await api.proposals(status);
      setItems(page.items);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't load proposals");
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    setLoading(true);
    void load();
    if (status !== "pending") return;
    const id = setInterval(() => void load(), 8000); // new proposals appear as the worker finishes
    return () => clearInterval(id);
  }, [load, status]);

  const remove = (id: string) => setItems((prev) => prev.filter((p) => p.id !== id));

  return (
    <div className="ingest">
      <div className="toolbar">
        <div className="segmented" role="tablist">
          {STATUSES.map((s) => (
            <button key={s} role="tab" aria-selected={s === status} className={s === status ? "active" : ""} onClick={() => setStatus(s)}>
              {titleCase(s)}
            </button>
          ))}
        </div>
        <button onClick={() => void load()}>Refresh</button>
      </div>

      {error && <p className="error">{error}</p>}
      {loading && <div className="skeleton block" />}
      {!loading && items.length === 0 && !error && (
        <div className="card pad empty-state">
          <strong>{status === "pending" ? "Nothing to review" : `No ${status} proposals`}</strong>
          <p className="muted">
            {status === "pending"
              ? "Add a call or email note from any lead to have budget, timeline, objections and sentiment extracted."
              : "Decisions you make will show up here."}
          </p>
        </div>
      )}
      {items.map((p) => (
        <ProposalCard key={p.id} proposal={p} onDecided={remove} />
      ))}

      {user?.role === "admin" && <DeadLetters />}
    </div>
  );
}

function ProposalCard({ proposal, onDecided }: { proposal: Proposal; onDecided: (id: string) => void }) {
  const pending = proposal.status === "pending";
  const changes: FieldChange[] = pending ? proposal.changes : (proposal.applied ?? []);
  const [selected, setSelected] = useState<Set<string>>(() => new Set(proposal.changes.map((c) => c.field)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle(field: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (!next.delete(field)) next.add(field);
      return next;
    });
  }

  async function decide(action: "accept" | "reject") {
    setBusy(true);
    setError(null);
    try {
      await api.decide(proposal.id, action, action === "accept" ? [...selected] : undefined);
      onDecided(proposal.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't save your decision");
      setBusy(false);
    }
  }

  return (
    <article className="card proposal">
      <header>
        <div>
          <strong>{proposal.lead.name}</strong> <span className="muted">· {proposal.lead.company}</span>
        </div>
        <span className="badge">{titleCase(proposal.source)}</span>
      </header>

      <details>
        <summary>Original note</summary>
        <blockquote>{proposal.note_text}</blockquote>
      </details>

      {changes.length === 0 ? (
        <p className="muted">
          {pending ? "No field changes. Accepting will just log the note on the lead." : "No fields were changed."}
        </p>
      ) : (
        <table className="diff">
          <thead>
            <tr>
              {pending && <th aria-label="Apply"></th>}
              <th>Field</th>
              <th>Current</th>
              <th>Proposed</th>
            </tr>
          </thead>
          <tbody>
            {changes.map((c) => (
              <tr key={c.field}>
                {pending && (
                  <td>
                    <input type="checkbox" checked={selected.has(c.field)} onChange={() => toggle(c.field)} aria-label={`Apply ${c.label}`} />
                  </td>
                )}
                <td>{c.label}</td>
                <td className="old">{show(c.field, c.current)}</td>
                <td className="new">{show(c.field, c.proposed)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {error && <p className="error" role="alert">{error}</p>}
      <footer>
        <span className="meta">Extracted with {proposal.cost_usd > 0 ? formatCost(proposal.cost_usd) : "no"} model cost</span>
        {pending && (
          <div className="actions">
            <button onClick={() => void decide("reject")} disabled={busy}>Reject</button>
            <button className="primary" onClick={() => void decide("accept")} disabled={busy || (changes.length > 0 && selected.size === 0)}>
              {changes.length === 0 ? "Log note" : `Accept ${selected.size === changes.length ? "all" : `${selected.size} selected`}`}
            </button>
          </div>
        )}
      </footer>
    </article>
  );
}

function DeadLetters() {
  const [letters, setLetters] = useState<DeadLetter[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLetters(await api.deadLetters());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't load the dead-letter queue");
    }
  }, []);
  useEffect(() => void load(), [load]);

  async function retry(id: string) {
    try {
      await api.retryDeadLetter(id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Retry failed");
    }
  }

  if (letters.length === 0 && !error) return null;
  return (
    <section className="card pad dlq">
      <h3>Failed extractions <span className="count">{letters.length}</span></h3>
      <p className="muted small">These notes failed after all retries. Retrying puts them back on the queue.</p>
      {error && <p className="error">{error}</p>}
      <ul>
        {letters.map((l) => (
          <li key={l.id}>
            <span>Lead #{l.lead_id} · {l.source} · {l.attempts} attempts<br /><span className="muted small">{l.error}</span></span>
            <button onClick={() => void retry(l.id)}>Retry</button>
          </li>
        ))}
      </ul>
    </section>
  );
}
