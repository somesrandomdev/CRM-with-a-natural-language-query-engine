import { useCallback, useEffect, useState, type DragEvent } from "react";
import { api, ApiError } from "../api";
import { formatCompact, formatUsd, relativeDays, titleCase } from "../format";
import { STAGES, type Lead, type Stage } from "../types";

const PAGE = 100;

type Columns = Record<Stage, { items: Lead[]; total: number }>;

const empty = (): Columns => ({
  new: { items: [], total: 0 },
  qualified: { items: [], total: 0 },
  demo: { items: [], total: 0 },
  proposal: { items: [], total: 0 },
  negotiation: { items: [], total: 0 },
  won: { items: [], total: 0 },
  lost: { items: [], total: 0 },
});

function sumBudget(leads: Lead[]): number {
  return leads.reduce((acc, l) => acc + (l.budget_usd ? Number(l.budget_usd) : 0), 0);
}

export default function Kanban({ onOpenLead }: { onOpenLead: (lead: Lead) => void }) {
  const [columns, setColumns] = useState<Columns>(empty);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState<Stage | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const pages = await Promise.all(STAGES.map((s) => api.listLeads({ page: 1, pageSize: PAGE, stage: s })));
      const next = empty();
      STAGES.forEach((s, i) => {
        const p = pages[i];
        if (p) next[s] = { items: p.items, total: p.total };
      });
      setColumns(next);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't load the pipeline");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function move(leadId: number, from: Stage, to: Stage) {
    if (from === to) return;
    const lead = columns[from].items.find((l) => l.id === leadId);
    if (!lead) return;
    const snapshot = columns;
    // Optimistic update, rolled back if the server refuses.
    setColumns({
      ...columns,
      [from]: { items: columns[from].items.filter((l) => l.id !== leadId), total: columns[from].total - 1 },
      [to]: { items: [{ ...lead, stage: to }, ...columns[to].items], total: columns[to].total + 1 },
    });
    try {
      await api.setStage(leadId, to);
    } catch (err) {
      setColumns(snapshot);
      setToast(err instanceof ApiError ? err.message : "Couldn't move the lead");
      setTimeout(() => setToast(null), 4000);
    }
  }

  function onDrop(e: DragEvent, to: Stage) {
    e.preventDefault();
    setDragOver(null);
    const raw = e.dataTransfer.getData("application/x-clearpipe-lead");
    if (!raw) return;
    const { id, from } = JSON.parse(raw) as { id: number; from: Stage };
    void move(id, from, to);
  }

  if (error)
    return (
      <div className="card pad">
        <p className="error">{error}</p>
        <button onClick={() => void load()}>Retry</button>
      </div>
    );

  return (
    <div className="kanban-wrap">
      {toast && <div className="toast" role="alert">{toast}</div>}
      <div className="kanban" aria-busy={loading}>
        {STAGES.map((stage) => {
          const col = columns[stage];
          return (
            <section
              key={stage}
              className={`column${dragOver === stage ? " drag-over" : ""}`}
              aria-label={`${titleCase(stage)} stage`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(stage);
              }}
              onDragLeave={() => setDragOver((s) => (s === stage ? null : s))}
              onDrop={(e) => onDrop(e, stage)}
            >
              <header>
                <h3>
                  <span className={`dot stage-${stage}`} aria-hidden /> {titleCase(stage)}
                </h3>
                <span className="count">{col.total}</span>
              </header>
              <div className="col-sub muted">
                {loading ? "…" : `${formatCompact(sumBudget(col.items))} USD${col.total > col.items.length ? "+" : ""}`}
              </div>
              <div className="cards">
                {col.items.map((lead) => (
                  <article
                    key={lead.id}
                    className="lead-card"
                    draggable
                    onDragStart={(e) => {
                      e.dataTransfer.setData("application/x-clearpipe-lead", JSON.stringify({ id: lead.id, from: stage }));
                      e.dataTransfer.effectAllowed = "move";
                    }}
                  >
                    <button className="card-main" onClick={() => onOpenLead(lead)}>
                      <strong>{lead.first_name} {lead.last_name}</strong>
                      <span className="muted">{lead.company.name}</span>
                      <span className="card-foot">
                        <span>{formatUsd(lead.budget_usd)}</span>
                        <span className="muted">{relativeDays(lead.last_contacted_at)}</span>
                      </span>
                    </button>
                    <label className="move">
                      <span className="sr-only">Move {lead.first_name} to stage</span>
                      <select value={stage} onChange={(e) => void move(lead.id, stage, e.target.value as Stage)}>
                        {STAGES.map((s) => (
                          <option key={s} value={s}>{titleCase(s)}</option>
                        ))}
                      </select>
                    </label>
                  </article>
                ))}
                {!loading && col.items.length === 0 && <p className="muted empty">No leads</p>}
                {col.total > col.items.length && (
                  <p className="muted empty">Showing {col.items.length} of {col.total}. Use Leads to browse the rest.</p>
                )}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
