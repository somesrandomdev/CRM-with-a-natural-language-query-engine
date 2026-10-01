import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartSpec } from "../chart";
import { formatCell, formatCompact, titleCase } from "../format";
import type { Cell as CellValue } from "../types";

// Distinguishable in both themes and for common colour-vision deficiencies.
const PALETTE = ["#4f6df5", "#14a3a8", "#e8a23a", "#d9534f", "#8e6bd8", "#3fa66b", "#c75b9b", "#7c8798"];

const tick = { fontSize: 12, fill: "var(--muted)" };

function xLabel(v: CellValue): string {
  return typeof v === "string" ? (/^\d{4}-\d{2}-\d{2}T/.test(v) ? v.slice(0, 7) : v) : String(v ?? "—");
}

export default function ResultChart({ spec }: { spec: ChartSpec }) {
  if (spec.kind === "number") {
    return (
      <div className="stat" role="img" aria-label={`${titleCase(spec.label)}: ${formatCell(spec.value)}`}>
        <div className="stat-value">{formatCell(spec.value)}</div>
        <div className="stat-label">{titleCase(spec.label)}</div>
      </div>
    );
  }

  const data = spec.data.map((d) => ({ ...d }));

  return (
    <div className="chart" aria-hidden={false}>
      <ResponsiveContainer width="100%" height={280}>
        {spec.kind === "bar" ? (
          <BarChart data={data} margin={{ top: 8, right: 12, bottom: 8, left: 4 }}>
            <CartesianGrid stroke="var(--border)" vertical={false} />
            <XAxis dataKey={spec.xKey} tick={tick} tickFormatter={xLabel} interval="preserveStartEnd" />
            <YAxis tick={tick} tickFormatter={(v: number) => formatCompact(v)} width={48} />
            <Tooltip formatter={(v) => formatCell(v as CellValue)} labelFormatter={(l) => xLabel(l as CellValue)} />
            {spec.seriesKeys.length > 1 && <Legend />}
            {spec.seriesKeys.map((k, i) => (
              <Bar key={k} dataKey={k} name={titleCase(k)} fill={PALETTE[i % PALETTE.length]} radius={[4, 4, 0, 0]} />
            ))}
          </BarChart>
        ) : spec.kind === "line" ? (
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 8, left: 4 }}>
            <CartesianGrid stroke="var(--border)" vertical={false} />
            <XAxis dataKey={spec.xKey} tick={tick} tickFormatter={xLabel} interval="preserveStartEnd" />
            <YAxis tick={tick} tickFormatter={(v: number) => formatCompact(v)} width={48} />
            <Tooltip formatter={(v) => formatCell(v as CellValue)} labelFormatter={(l) => xLabel(l as CellValue)} />
            {spec.seriesKeys.length > 1 && <Legend />}
            {spec.seriesKeys.map((k, i) => (
              <Line key={k} type="monotone" dataKey={k} name={titleCase(k)} stroke={PALETTE[i % PALETTE.length]} strokeWidth={2} dot={data.length < 20} />
            ))}
          </LineChart>
        ) : (
          <PieChart>
            <Tooltip formatter={(v) => formatCell(v as CellValue)} />
            <Legend />
            <Pie data={data} dataKey={spec.valueKey} nameKey={spec.nameKey} innerRadius={56} outerRadius={104} paddingAngle={2}>
              {data.map((_, i) => (
                <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
              ))}
            </Pie>
          </PieChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
