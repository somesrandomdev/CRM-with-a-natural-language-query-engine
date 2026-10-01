import type { Cell } from "./types";

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export function formatUsd(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? usd.format(n) : "—";
}

export function formatCompact(n: number): string {
  return compact.format(n);
}

export function formatCost(usdAmount: number): string {
  if (usdAmount === 0) return "$0";
  return usdAmount < 0.01 ? `$${usdAmount.toFixed(4)}` : `$${usdAmount.toFixed(3)}`;
}

const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

/** Render a result-table cell. Numbers get grouping; ISO timestamps become dates. */
export function formatCell(cell: Cell): string {
  if (cell === null) return "—";
  if (typeof cell === "number") return Number.isInteger(cell) ? cell.toLocaleString("en-US") : cell.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (typeof cell === "boolean") return cell ? "yes" : "no";
  if (ISO_DATETIME.test(cell)) return cell.slice(0, 10);
  return cell;
}

export function formatDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

export function titleCase(s: string): string {
  return s.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export function relativeDays(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "never";
  const days = Math.floor((now.getTime() - new Date(iso).getTime()) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days}d ago`;
}
