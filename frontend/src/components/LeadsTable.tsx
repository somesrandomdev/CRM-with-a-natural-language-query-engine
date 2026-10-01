import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { formatDate, formatUsd } from "../format";
import { useDebounced, useLatest } from "../hooks";
import { STAGES, type Lead, type LeadPage, type Stage } from "../types";
import StageChip from "./StageChip";

const PAGE_SIZE = 25;

export default function LeadsTable({ onOpenLead }: { onOpenLead: (lead: Lead) => void }) {
  const [search, setSearch] = useState("");
  const [stage, setStage] = useState<Stage | "">("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<LeadPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const q = useDebounced(search.trim(), 300);
  const latest = useLatest();

  // A new search or filter always starts from page 1.
  useEffect(() => setPage(1), [q, stage]);

  const load = useCallback(async () => {
    const id = latest.next();
    setLoading(true);
    setError(null);
    try {
      const result = await api.listLeads({ page, pageSize: PAGE_SIZE, q, stage });
      if (latest.isCurrent(id)) setData(result);
    } catch (err) {
      if (latest.isCurrent(id)) setError(err instanceof ApiError ? err.message : "Couldn't load leads");
    } finally {
      if (latest.isCurrent(id)) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, q, stage]);

  useEffect(() => {
    void load();
  }, [load]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  const from = data && data.total > 0 ? (page - 1) * PAGE_SIZE + 1 : 0;
  const to = data ? Math.min(page * PAGE_SIZE, data.total) : 0;

  return (
    <div className="card">
      <div className="toolbar">
        <input
          type="search"
          placeholder="Search name, email or company"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          aria-label="Search leads"
        />
        <select value={stage} onChange={(e) => setStage(e.target.value as Stage | "")} aria-label="Filter by stage">
          <option value="">All stages</option>
          {STAGES.map((s) => (
            <option key={s} value={s}>{s[0]?.toUpperCase()}{s.slice(1)}</option>
          ))}
        </select>
      </div>

      {error ? (
        <div className="pad">
          <p className="error">{error}</p>
          <button onClick={() => void load()}>Retry</button>
        </div>
      ) : (
        <div className={`table-wrap${loading ? " loading" : ""}`} aria-busy={loading}>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Company</th>
                <th>Stage</th>
                <th className="num">Budget</th>
                <th>Source</th>
                <th>Created</th>
                <th>Last contact</th>
              </tr>
            </thead>
            <tbody>
              {data?.items.map((l) => (
                <tr key={l.id} className="clickable" onClick={() => onOpenLead(l)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onOpenLead(l)}>
                  <td>
                    <strong>{l.first_name} {l.last_name}</strong>
                    <div className="muted small">{l.job_title ?? l.email}</div>
                  </td>
                  <td>{l.company.name}</td>
                  <td><StageChip stage={l.stage} /></td>
                  <td className="num">{formatUsd(l.budget_usd)}</td>
                  <td>{l.source}</td>
                  <td>{formatDate(l.created_at)}</td>
                  <td>{formatDate(l.last_contacted_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data && data.items.length === 0 && !loading && (
            <p className="muted pad">{q || stage ? "No leads match your filters." : "No leads yet."}</p>
          )}
        </div>
      )}

      <div className="pager">
        <span className="muted">{data ? `${from}–${to} of ${data.total}` : "…"}</span>
        <div>
          <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1 || loading}>Previous</button>
          <span className="page-num">Page {page} of {totalPages}</span>
          <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page >= totalPages || loading}>Next</button>
        </div>
      </div>
    </div>
  );
}
