import { describe, expect, it } from "vitest";
import { chartSpec } from "./chart";
import { parseError as parseErrorForTest } from "./api";
import { formatCell, formatCost, formatUsd } from "./format";

describe("chartSpec", () => {
  const base = { columns: ["stage", "n"], rows: [["won", 3], ["new", 5]] as (string | number | null)[][] };

  it("draws bars with the text column as category and numeric columns as series", () => {
    const spec = chartSpec({ ...base, chart_hint: "bar" });
    expect(spec).toMatchObject({ kind: "bar", xKey: "stage", seriesKeys: ["n"] });
  });

  it("falls back to the table when the hint does not fit the data", () => {
    expect(chartSpec({ ...base, chart_hint: "table" })).toBeNull();
    expect(chartSpec({ columns: ["a", "b"], rows: [["x", "y"]], chart_hint: "bar" })).toBeNull();
    expect(chartSpec({ ...base, rows: [], chart_hint: "bar" })).toBeNull();
    expect(chartSpec({ columns: ["stage", "n"], rows: [["won", 1]], chart_hint: "line" })).toBeNull();
  });

  it("number needs exactly one row and a numeric value", () => {
    expect(chartSpec({ columns: ["n"], rows: [[42]], chart_hint: "number" })).toEqual({ kind: "number", label: "n", value: 42 });
    expect(chartSpec({ columns: ["n"], rows: [[1], [2]], chart_hint: "number" })).toBeNull();
    expect(chartSpec({ columns: ["n"], rows: [[null]], chart_hint: "number" })).toBeNull();
  });

  it("pie refuses a single slice, too many slices, and negatives", () => {
    expect(chartSpec({ ...base, chart_hint: "pie" })).toMatchObject({ kind: "pie", nameKey: "stage", valueKey: "n" });
    expect(chartSpec({ columns: ["s", "n"], rows: [["a", 1]], chart_hint: "pie" })).toBeNull();
    const many = Array.from({ length: 13 }, (_, i) => [`s${i}`, i + 1]);
    expect(chartSpec({ columns: ["s", "n"], rows: many, chart_hint: "pie" })).toBeNull();
    expect(chartSpec({ columns: ["s", "n"], rows: [["a", -1], ["b", 2]], chart_hint: "pie" })).toBeNull();
  });

  it("treats a column with nulls and numbers as numeric, an all-null column as not", () => {
    const spec = chartSpec({ columns: ["m", "v"], rows: [["a", null], ["b", 2]], chart_hint: "bar" });
    expect(spec).toMatchObject({ seriesKeys: ["v"] });
    expect(chartSpec({ columns: ["m", "v"], rows: [["a", null], ["b", null]], chart_hint: "bar" })).toBeNull();
  });

  it("supports several series", () => {
    const spec = chartSpec({ columns: ["month", "won", "lost"], rows: [["2026-01", 1, 2], ["2026-02", 3, 4]], chart_hint: "line" });
    expect(spec).toMatchObject({ kind: "line", xKey: "month", seriesKeys: ["won", "lost"] });
  });
});

describe("formatting", () => {
  it("formats cells", () => {
    expect(formatCell(null)).toBe("—");
    expect(formatCell(1234567)).toBe("1,234,567");
    expect(formatCell(12.3456)).toBe("12.35");
    expect(formatCell("2026-05-01T12:00:00+00:00")).toBe("2026-05-01");
    expect(formatCell("hello")).toBe("hello");
    expect(formatCell(true)).toBe("yes");
  });
  it("formats money and cost", () => {
    expect(formatUsd("25000.00")).toBe("$25,000");
    expect(formatUsd(null)).toBe("—");
    expect(formatUsd("abc")).toBe("—");
    expect(formatCost(0)).toBe("$0");
    expect(formatCost(0.0073)).toBe("$0.0073");
    expect(formatCost(0.1234)).toBe("$0.123");
  });
});

describe("parseError", () => {
  it("handles FastAPI and query-endpoint error shapes", () => {
    expect(parseErrorForTest(401, { detail: "Invalid or expired credentials" }).message).toBe("Invalid or expired credentials");
    const q = parseErrorForTest(422, { error: { code: "invalid_ir", message: "Try rephrasing", details: ["a", "b"] }, cost_usd: 0.012 });
    expect([q.code, q.details, q.costUsd]).toEqual(["invalid_ir", ["a", "b"], 0.012]);
    expect(parseErrorForTest(422, { detail: [{ msg: "field required" }] }).message).toBe("field required");
    expect(parseErrorForTest(500, null).message).toContain("500");
  });
});
