import { formatCell, titleCase } from "../format";
import type { Cell } from "../types";

export default function ResultTable({ columns, rows }: { columns: string[]; rows: Cell[][] }) {
  if (rows.length === 0) return <p className="muted pad">No rows matched.</p>;
  // Right-align a column (header included) when every value in it is a number.
  const numeric = columns.map((_, i) => rows.every((r) => r[i] === null || typeof r[i] === "number"));
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th key={c} className={numeric[i] ? "num" : undefined}>
                {titleCase(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j} className={numeric[j] ? "num" : undefined}>
                  {formatCell(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
