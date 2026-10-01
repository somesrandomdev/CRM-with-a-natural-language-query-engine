import type { Cell, ChartHint, QueryResponse } from "./types";

export type ChartSpec =
  | { kind: "number"; label: string; value: number }
  | { kind: "bar"; xKey: string; seriesKeys: string[]; data: Record<string, Cell>[] }
  | { kind: "line"; xKey: string; seriesKeys: string[]; data: Record<string, Cell>[] }
  | { kind: "pie"; nameKey: string; valueKey: string; data: Record<string, Cell>[] };

const MAX_PIE_SLICES = 12;

const isNumber = (v: Cell): v is number => typeof v === "number" && Number.isFinite(v);

function numericColumns(columns: string[], rows: Cell[][]): number[] {
  return columns
    .map((_, i) => i)
    .filter((i) => rows.length > 0 && rows.every((r) => r[i] === null || isNumber(r[i] as Cell)) && rows.some((r) => isNumber(r[i] as Cell)));
}

/**
 * Decide how to draw a result. The model's `chart_hint` is only a suggestion: if the data's shape
 * can't support it (no numeric column, one row for a line, too many pie slices) we return null and
 * the caller shows the table alone.
 */
export function chartSpec(result: Pick<QueryResponse, "columns" | "rows" | "chart_hint">): ChartSpec | null {
  const { columns, rows } = result;
  const hint: ChartHint = result.chart_hint;
  if (hint === "table" || rows.length === 0) return null;

  const numeric = numericColumns(columns, rows);

  if (hint === "number") {
    const first = rows[0]?.[numeric[0] ?? -1];
    if (rows.length !== 1 || numeric.length === 0 || first === undefined || !isNumber(first)) return null;
    return { kind: "number", label: columns[numeric[0] as number] as string, value: first };
  }

  const categoryIdx = columns.map((_, i) => i).find((i) => !numeric.includes(i));
  if (categoryIdx === undefined || numeric.length === 0) return null;
  const xKey = columns[categoryIdx] as string;
  const seriesKeys = numeric.map((i) => columns[i] as string);
  const data = rows.map((r) => Object.fromEntries(columns.map((c, i) => [c, r[i] ?? null])) as Record<string, Cell>);

  if (hint === "pie") {
    if (rows.length < 2 || rows.length > MAX_PIE_SLICES) return null;
    const valueKey = seriesKeys[0] as string;
    if (data.some((d) => ((d[valueKey] as number | null) ?? 0) < 0)) return null; // pies can't show negatives
    return { kind: "pie", nameKey: xKey, valueKey, data };
  }
  if (hint === "line" && rows.length < 2) return null;
  return hint === "line" ? { kind: "line", xKey, seriesKeys, data } : { kind: "bar", xKey, seriesKeys, data };
}
