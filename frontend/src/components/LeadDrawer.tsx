import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { formatCost, formatDate, formatUsd, titleCase } from "../format";
import type { Lead, Score } from "../types";
import StageChip from "./StageChip";

export default function LeadDrawer({ lead, onClose }: { lead: Lead; onClose: () => void }) {
  const [score, setScore] = useState<Score | null>(null);
  const [scoreError, setScoreError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setScore(null);
    setScoreError(null);
    api
      .score(lead.id)
      .then((s) => !cancelled && setScore(s))
      .catch((e) => !cancelled && setScoreError(e instanceof ApiError ? e.message : "Couldn't load the score"));
    return () => {
      cancelled = true;
    };
  }, [lead.id]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`${lead.first_name} ${lead.last_name}`} onClick={(e) => e.stopPropagation()}>
        <header>
          <div>
            <h2>{lead.first_name} {lead.last_name}</h2>
            <p className="muted">{lead.job_title ? `${lead.job_title} · ` : ""}{lead.company.name}</p>
          </div>
          <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <dl className="facts">
          <div><dt>Stage</dt><dd><StageChip stage={lead.stage} /></dd></div>
          <div><dt>Budget</dt><dd>{formatUsd(lead.budget_usd)}</dd></div>
          <div><dt>Email</dt><dd>{lead.email}</dd></div>
          <div><dt>Source</dt><dd>{titleCase(lead.source)}</dd></div>
          <div><dt>Created</dt><dd>{formatDate(lead.created_at)}</dd></div>
          <div><dt>Last contact</dt><dd>{formatDate(lead.last_contacted_at)}</dd></div>
          {lead.timeline && <div><dt>Timeline</dt><dd>{lead.timeline}</dd></div>}
          {lead.sentiment && <div><dt>Sentiment</dt><dd>{titleCase(lead.sentiment)}</dd></div>}
          {lead.objections && lead.objections.length > 0 && (
            <div><dt>Objections</dt><dd>{lead.objections.join("; ")}</dd></div>
          )}
        </dl>

        <section>
          <h3>Lead score</h3>
          {scoreError && <p className="error">{scoreError}</p>}
          {!score && !scoreError && <div className="skeleton block short" />}
          {score && (
            <>
              <div className="score-head">
                <span className="score-num">{score.score}</span>
                <span className="muted">/ 100</span>
              </div>
              <p className="rationale">{score.rationale}</p>
              <ul className="breakdown">
                {score.breakdown.map((b) => (
                  <li key={b.rule}>
                    <div className="bd-row">
                      <span>{titleCase(b.rule)}</span>
                      <span className="muted">{b.points}/{b.max_points}</span>
                    </div>
                    <div className="bar" role="presentation"><div style={{ width: `${(b.points / b.max_points) * 100}%` }} /></div>
                    <div className="muted small">{b.detail}</div>
                  </li>
                ))}
              </ul>
              <p className="meta">Score from fixed rules; the wording is {score.rationale_source === "llm" ? "AI-written" : "auto-generated"} ({formatCost(score.cost_usd)}).</p>
            </>
          )}
        </section>

        <NoteForm leadId={lead.id} />
      </aside>
    </div>
  );
}

function NoteForm({ leadId }: { leadId: number }) {
  const [source, setSource] = useState<"call" | "email" | "note">("call");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  // One key per draft: resubmitting the same text after a network hiccup can't create a duplicate.
  const [key, setKey] = useState(() => crypto.randomUUID());

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const job = await api.submitNote(leadId, source, text, key);
      setMessage({
        kind: "ok",
        text: job.idempotent_replay
          ? "That note was already queued."
          : "Queued. Review the extracted fields in the Ingest tab in a moment.",
      });
      setText("");
      setKey(crypto.randomUUID());
    } catch (err) {
      setMessage({ kind: "error", text: err instanceof ApiError ? err.message : "Couldn't submit the note" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h3>Add a call or email note</h3>
      <form onSubmit={submit} className="note-form">
        <select value={source} onChange={(e) => setSource(e.target.value as typeof source)} aria-label="Note type">
          <option value="call">Call</option>
          <option value="email">Email</option>
          <option value="note">Other note</option>
        </select>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          maxLength={20000}
          placeholder="Paste the call notes or email text. Budget, timeline, objections and sentiment will be extracted for your review."
          aria-label="Note text"
        />
        <button className="primary" disabled={busy || !text.trim()}>{busy ? "Submitting…" : "Extract signals"}</button>
        {message && <p className={message.kind === "ok" ? "ok" : "error"} role="status">{message.text}</p>}
      </form>
    </section>
  );
}
