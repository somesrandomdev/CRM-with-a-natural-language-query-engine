import type {
  DeadLetter,
  JobOut,
  Lead,
  LeadPage,
  ProposalPage,
  ProposalStatus,
  Proposal,
  QueryResponse,
  Score,
  Stage,
  User,
} from "./types";

const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";
const TOKEN_KEY = "clearpipe.token";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
    public details: string[] = [],
    public costUsd?: number,
  ) {
    super(message);
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the session simply won't survive a reload */
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

/** Turn FastAPI's two error shapes ({detail} and the query endpoint's {error, cost_usd}) into one. */
export function parseError(status: number, body: unknown): ApiError {
  const b = (body ?? {}) as Record<string, unknown>;
  const err = b.error as { code?: string; message?: string; details?: string[] } | undefined;
  if (err) return new ApiError(status, err.message ?? "Request failed", err.code, err.details ?? [], b.cost_usd as number | undefined);
  const detail = b.detail;
  if (typeof detail === "string") return new ApiError(status, detail);
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => (d as { msg?: string }).msg ?? "invalid input");
    return new ApiError(status, msgs[0] ?? "Invalid request", undefined, msgs);
  }
  return new ApiError(status, `Request failed (HTTP ${status})`);
}

async function request<T>(
  path: string,
  init: { method?: string; body?: unknown; headers?: Record<string, string>; form?: URLSearchParams } = {},
): Promise<T> {
  const headers: Record<string, string> = { ...init.headers };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (init.form) {
    body = init.form;
  } else if (init.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.body);
  }
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, { method: init.method ?? "GET", headers, body });
  } catch {
    throw new ApiError(0, "Can't reach the Clearpipe API. Is it running?");
  }
  const text = await res.text();
  const data: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    if (res.status === 401 && path !== "/auth/login") onUnauthorized();
    throw parseError(res.status, data);
  }
  return data as T;
}

export const api = {
  async login(email: string, password: string): Promise<string> {
    const form = new URLSearchParams({ username: email, password });
    const out = await request<{ access_token: string }>("/auth/login", { method: "POST", form });
    return out.access_token;
  },
  me: () => request<User>("/auth/me"),

  listLeads(params: { page: number; pageSize: number; q?: string; stage?: Stage | "" }): Promise<LeadPage> {
    const qs = new URLSearchParams({ page: String(params.page), page_size: String(params.pageSize) });
    if (params.q) qs.set("q", params.q);
    if (params.stage) qs.set("stage", params.stage);
    return request<LeadPage>(`/leads?${qs}`);
  },
  setStage: (id: number, stage: Stage) =>
    request<Lead>(`/leads/${id}`, { method: "PATCH", body: { stage } }),
  score: (id: number) => request<Score>(`/leads/${id}/score`),

  query: (question: string) => request<QueryResponse>("/query", { method: "POST", body: { question } }),

  submitNote: (leadId: number, source: "call" | "email" | "note", text: string, key: string) =>
    request<JobOut>("/ingest/note", {
      method: "POST",
      body: { lead_id: leadId, source, text },
      headers: { "Idempotency-Key": key },
    }),
  proposals: (status: ProposalStatus) => request<ProposalPage>(`/ingest/proposals?status=${status}&limit=50`),
  decide: (id: string, action: "accept" | "reject", fields?: string[]) =>
    request<Proposal>(`/ingest/proposals/${id}`, { method: "PATCH", body: { action, fields } }),
  deadLetters: () => request<DeadLetter[]>("/ingest/dead-letters"),
  retryDeadLetter: (id: string) => request<JobOut>(`/ingest/dead-letters/${id}/retry`, { method: "POST" }),
};
