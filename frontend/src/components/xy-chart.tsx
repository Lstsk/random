"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  Scatter,
  ScatterChart,
  XAxis,
  YAxis,
} from "recharts";

import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import type { ChartVisualization, Datum, Evidence } from "@/lib/api";

const COLORS = Array.from({ length: 8 }, (_, i) => `var(--chart-${i + 1})`);
const SINGLE = "Trials";

type Row = Record<string, unknown> & { records: Record<string, Datum> };

/** One row per x value and one column per series (keys s0, s1… so they are CSS-safe),
 * keeping each source record for the evidence panel. */
function pivot(viz: ChartVisualization) {
  const x = viz.encoding.x.field;
  const nameOf = (d: Datum) => (viz.encoding.color ? String(d[viz.encoding.color.field]) : SINGLE);
  const names = [...new Set((viz.data as Datum[]).map(nameOf))];
  const keyOf = (d: Datum) => `s${names.indexOf(nameOf(d))}`;
  const rows = new Map<unknown, Row>();
  for (const d of viz.data as Datum[]) {
    const row = rows.get(d[x]) ?? { [x]: d[x], records: {} };
    row[keyOf(d)] = d.trial_count;
    row.records[keyOf(d)] = d;
    rows.set(d[x], row);
  }
  const config: ChartConfig = Object.fromEntries(
    names.map((name, i) => [`s${i}`, { label: name, color: COLORS[i % COLORS.length] }]),
  );
  return { rows: [...rows.values()], keys: Object.keys(config), config };
}

function evidenceAt(rows: Row[], config: ChartConfig, index: unknown, label: (d: Datum) => string): Evidence {
  // Recharts 3 reports the active index as a string.
  const row = index == null ? undefined : rows[Number(index)];
  if (!row) return [];
  return Object.entries(row.records).map(([key, datum]) => {
    const series = String(config[key]?.label ?? SINGLE);
    return { label: series === SINGLE ? label(datum) : `${label(datum)} · ${series}`, datum };
  });
}

export function XYChart({ viz, onSelect }: { viz: ChartVisualization; onSelect: (e: Evidence) => void }) {
  const { x, y } = viz.encoding;
  const { rows, keys, config } = pivot(viz);
  const select = (label: (d: Datum) => string) => (state: { activeTooltipIndex?: unknown }) =>
    onSelect(evidenceAt(rows, config, state.activeTooltipIndex, label));
  const legend = keys.length > 1 ? <ChartLegend content={<ChartLegendContent />} /> : null;

  if (viz.type === "bar" || viz.type === "grouped_bar") {
    const height = Math.max(240, rows.length * (keys.length > 1 ? 18 * keys.length + 12 : 30) + 60);
    return (
      <ChartContainer config={config} className="w-full" style={{ height }}>
        <BarChart data={rows} layout="vertical" margin={{ left: 8, right: 16 }} onClick={select((d) => String(d[x.field]))}>
          <CartesianGrid horizontal={false} />
          <XAxis type="number" allowDecimals={false} label={{ value: y.title, position: "insideBottom", offset: -4 }} />
          <YAxis type="category" dataKey={x.field} width={200} tickLine={false} interval={0} />
          <ChartTooltip content={<ChartTooltipContent />} />
          {legend}
          {keys.map((k) => (
            <Bar key={k} dataKey={k} fill={`var(--color-${k})`} radius={[0, 4, 4, 0]} isAnimationActive={false} className="cursor-pointer" />
          ))}
        </BarChart>
      </ChartContainer>
    );
  }

  if (viz.type === "time_series") {
    // Years that are not over (partial_period) or not started (projected) are drawn dashed so
    // their lower counts don't read as a decline. The dashed line starts at the last full year.
    const open = (row: Row) => Object.values(row.records).some((d) => d.partial_period || d.projected);
    const lines = rows.map((row, i) => {
      const line: Record<string, unknown> = { ...row };
      for (const k of keys) {
        if (open(row)) {
          line[`${k}_open`] = row[k];
          delete line[k];
        } else if (rows[i + 1] && open(rows[i + 1])) {
          line[`${k}_open`] = row[k];
        }
      }
      return line;
    });
    const yearLabel = (_: unknown, payload: readonly { payload?: Row }[]) => {
      const row = payload?.[0]?.payload;
      if (!row) return "";
      const d = Object.values(row.records)[0];
      const suffix = d?.projected ? " (planned starts)" : d?.partial_period ? " (year not over)" : "";
      return `${row[x.field]}${suffix}`;
    };
    const lineConfig: ChartConfig = {
      ...config,
      ...Object.fromEntries(keys.map((k) => [`${k}_open`, config[k]])),
    };
    return (
      <div className="space-y-2">
        <ChartContainer config={lineConfig} className="h-[360px] w-full">
          <LineChart data={lines} margin={{ left: 8, right: 16, top: 8 }} onClick={select((d) => String(d[x.field]))}>
            <CartesianGrid vertical={false} />
            <XAxis dataKey={x.field} tickLine={false} />
            <YAxis allowDecimals={false} width={48} />
            <ChartTooltip
              content={({ active, label, payload }) => (
                // The solid and dashed lines meet at one year; list each series once.
                <ChartTooltipContent
                  active={active}
                  label={label}
                  payload={payload?.filter(
                    (item, i, all) =>
                      all.findIndex((o) => String(o.dataKey).replace("_open", "") === String(item.dataKey).replace("_open", "")) === i,
                  )}
                  labelFormatter={yearLabel}
                />
              )}
            />
            {legend}
            {keys.map((k) => (
              <Line key={k} dataKey={k} stroke={`var(--color-${k})`} strokeWidth={2} dot={{ r: 3 }} activeDot={{ r: 5 }} isAnimationActive={false} />
            ))}
            {keys.map((k) => (
              <Line
                key={`${k}_open`}
                dataKey={`${k}_open`}
                stroke={`var(--color-${k})`}
                strokeWidth={2}
                strokeDasharray="5 4"
                dot={{ r: 3, fillOpacity: 0.3 }}
                activeDot={{ r: 5 }}
                legendType="none"
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ChartContainer>
        {rows.some(open) && (
          <p className="text-xs text-muted-foreground">
            Dashed: the current year (not over yet) and future years (planned start dates).
          </p>
        )}
      </div>
    );
  }

  if (viz.type === "histogram") {
    const bins = (viz.data as Datum[]).map((d) => ({ ...d, bin: `${d.bin_start}–${d.bin_end}` }));
    return (
      <ChartContainer config={{ trial_count: { label: SINGLE, color: COLORS[0] } }} className="h-[360px] w-full">
        <BarChart
          data={bins}
          barCategoryGap={2}
          onClick={(state) => {
            const bin = state.activeTooltipIndex == null ? undefined : bins[Number(state.activeTooltipIndex)];
            if (bin) onSelect([{ label: `${x.title}: ${bin.bin}`, datum: bin }]);
          }}
        >
          <CartesianGrid vertical={false} />
          <XAxis dataKey="bin" tickLine={false} label={{ value: x.title, position: "insideBottom", offset: -4 }} height={44} />
          <YAxis allowDecimals={false} width={48} />
          <ChartTooltip content={<ChartTooltipContent />} />
          <Bar dataKey="trial_count" fill="var(--color-trial_count)" radius={[4, 4, 0, 0]} isAnimationActive={false} className="cursor-pointer" />
        </BarChart>
      </ChartContainer>
    );
  }

  return (
    <ChartContainer config={{ points: { label: SINGLE, color: COLORS[0] } }} className="h-[400px] w-full">
      <ScatterChart margin={{ left: 8, right: 16, top: 8, bottom: 8 }}>
        <CartesianGrid />
        <XAxis type="number" dataKey={x.field} name={x.title} label={{ value: x.title, position: "insideBottom", offset: -4 }} height={44} />
        <YAxis type="number" dataKey={y.field} name={y.title} width={56} />
        <ChartTooltip content={<ChartTooltipContent hideLabel />} />
        <Scatter
          data={viz.data as Datum[]}
          fill="var(--color-points)"
          fillOpacity={0.7}
          isAnimationActive={false}
          className="cursor-pointer"
          onClick={(point) => {
            const d = (point as { payload?: Datum }).payload;
            if (d) onSelect([{ label: String(d.nct_id), datum: d }]);
          }}
        />
      </ScatterChart>
    </ChartContainer>
  );
}
