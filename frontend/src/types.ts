export type Role = "admin" | "rep";

export const STAGES = [
  "new",
  "qualified",
  "demo",
  "proposal",
  "negotiation",
  "won",
  "lost",
] as const;
export type Stage = (typeof STAGES)[number];

export interface User {
  id: number;
  email: string;
  full_name: string;
  role: Role;
}

export interface Company {
  id: number;
  name: string;
  industry: string;
  employee_count: number;
  country: string;
}

export interface Lead {
  id: number;
  first_name: string;
  last_name: string;
  email: string;
  job_title: string | null;
  stage: Stage;
  source: string;
  budget_usd: string | null; // Decimal serialised as a string
  created_at: string;
  last_contacted_at: string | null;
  closed_at: string | null;
  timeline: string | null;
  sentiment: "positive" | "neutral" | "negative" | null;
  objections: string[] | null;
  company: Company;
  owner_id: number;
}

export interface LeadPage {
  items: Lead[];
  total: number;
  page: number;
  page_size: number;
}

export type Cell = string | number | boolean | null;

export type ChartHint = "table" | "bar" | "line" | "pie" | "number";

export interface QueryResponse {
  question: string;
  ir: Record<string, unknown>;
  sql: string;
  columns: string[];
  rows: Cell[][];
  row_count: number;
  truncated: boolean;
  chart_hint: ChartHint;
  explanation: string;
  explanation_source: "llm" | "fallback";
  elapsed_ms: number;
  cost_usd: number;
}

export interface FieldChange {
  field: string;
  label: string;
  current: unknown;
  proposed: unknown;
}

export type ProposalStatus = "pending" | "accepted" | "rejected";

export interface Proposal {
  id: string;
  job_id: string;
  status: ProposalStatus;
  source: "call" | "email" | "note";
  note_text: string;
  lead: { id: number; name: string; company: string };
  extracted: Record<string, unknown>;
  changes: FieldChange[];
  applied: FieldChange[] | null;
  created_at: string;
  decided_at: string | null;
  cost_usd: number;
}

export interface ProposalPage {
  items: Proposal[];
  total: number;
}

export interface DeadLetter {
  id: string;
  job_id: string;
  lead_id: number;
  error: string;
  attempts: number;
  source: string;
  created_at: string;
}

export interface ScoreBreakdownItem {
  rule: string;
  points: number;
  max_points: number;
  detail: string;
}

export interface Score {
  lead_id: number;
  score: number;
  breakdown: ScoreBreakdownItem[];
  rationale: string;
  rationale_source: "llm" | "fallback";
  cost_usd: number;
}

export interface JobOut {
  id: string;
  lead_id: number;
  status: string;
  attempts: number;
  idempotent_replay: boolean;
}
