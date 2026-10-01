"use client";

import { useState } from "react";

import { NetworkGraph } from "@/components/network-graph";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { XYChart } from "@/components/xy-chart";
import type { Citation, Datum, Evidence, Meta, VisualizeResponse } from "@/lib/api";

const CT_GOV = "https://clinicaltrials.gov/study/";

export function ResultView({ response }: { response: VisualizeResponse }) {
  const [evidence, setEvidence] = useState<Evidence>([]);
  const viz = response.visualization!;
  const meta = response.meta!;
  const network = viz.type === "network";
  const rows: Datum[] = network ? (viz.data.edges as Datum[]) : (viz.data as Datum[]);
  const columns = network
    ? ["source", "target", "trial_count"]
    : [viz.encoding.x.field, viz.encoding.x2?.field, viz.encoding.color?.field, viz.encoding.y.field].filter(
        (c): c is string => Boolean(c),
      );

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{viz.title}</CardTitle>
          <CardDescription>Click a bar, point, node or link to see the trials behind it.</CardDescription>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="chart">
            <TabsList>
              <TabsTrigger value="chart">Chart</TabsTrigger>
              <TabsTrigger value="table">Table</TabsTrigger>
              <TabsTrigger value="json">JSON</TabsTrigger>
            </TabsList>
            <TabsContent value="chart" className="pt-2">
              {network ? <NetworkGraph viz={viz} onSelect={setEvidence} /> : <XYChart viz={viz} onSelect={setEvidence} />}
            </TabsContent>
            <TabsContent value="table">
              <ScrollArea className="h-[480px]">
                <Table>
                  <TableHeader>
                    <TableRow>
                      {columns.map((c) => (
                        <TableHead key={c} className={c === "trial_count" ? "text-right" : ""}>
                          {c.replaceAll("_", " ")}
                        </TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((row, i) => (
                      <TableRow key={i} className="cursor-pointer" onClick={() => setEvidence([{ label: columns.map((c) => row[c]).join(" · "), datum: row }])}>
                        {columns.map((c) => (
                          <TableCell key={c} className={c === "trial_count" ? "text-right tabular-nums" : ""}>
                            {String(row[c] ?? "")}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </ScrollArea>
            </TabsContent>
            <TabsContent value="json">
              <ScrollArea className="h-[480px] rounded-md border bg-muted/40">
                <pre className="p-3 text-xs">{JSON.stringify(response, null, 2)}</pre>
              </ScrollArea>
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <EvidencePanel evidence={evidence} />
        <MetaPanel meta={meta} />
      </div>
    </div>
  );
}

function EvidencePanel({ evidence }: { evidence: Evidence }) {
  const link = (id: string) => (
    <a key={id} href={CT_GOV + id} target="_blank" rel="noopener noreferrer" className="text-primary underline-offset-2 hover:underline">
      {id}
    </a>
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Evidence</CardTitle>
        <CardDescription>Trials behind the selected data, with the exact registry fields that put them there.</CardDescription>
      </CardHeader>
      <CardContent>
        {evidence.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing selected yet.</p>
        ) : (
          <ScrollArea className="h-[360px] pr-3">
            <div className="space-y-5 text-sm">
              {evidence.map(({ label, datum }) => {
                const ids = datum.nct_ids ?? [];
                // With server counts, trial_count is exact and nct_ids is a sample.
                const total = typeof datum.trial_count === "number" ? datum.trial_count : ids.length;
                const sampled = ids.length < total;
                const byTrial = Object.groupBy(datum.citations ?? [], (c) => c.nct_id);
                return (
                  <div key={label} className="space-y-2">
                    <p>
                      <span className="font-medium">{label}</span>{" "}
                      <Badge variant="secondary">{total.toLocaleString()} trials</Badge>
                    </p>
                    {typeof datum.source_query === "string" && (
                      <p className="text-xs">
                        <a href={datum.source_query} target="_blank" rel="noopener noreferrer" className="text-primary underline-offset-2 hover:underline">
                          Check this count on the ClinicalTrials.gov API
                        </a>{" "}
                        <span className="text-muted-foreground">(its totalCount is this number)</span>
                      </p>
                    )}
                    {Object.entries(byTrial).map(([id, cites = []]) => (
                      <TrialCitations key={id} link={link(id)} citations={cites} />
                    ))}
                    {ids.length > 0 && (
                      <p className="text-xs text-muted-foreground">
                        {sampled ? "Example trials" : "All trials"}:{" "}
                        {ids.slice(0, 40).map((id, i) => [i > 0 && ", ", link(id)])}
                        {ids.length > 40 && ` and ${ids.length - 40} more`}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          </ScrollArea>
        )}
      </CardContent>
    </Card>
  );
}

/** One cited trial: its title, the fields that place it in the datum, and (collapsed) the
 * fields that show it matches the question's filters. */
function TrialCitations({ link, citations }: { link: React.ReactNode; citations: Citation[] }) {
  const title = citations.find((c) => c.kind === "title");
  const grouping = citations.filter((c) => (c.kind ?? "grouping") === "grouping");
  const filter = citations.filter((c) => c.kind === "filter");
  const field = (c: Citation) => (
    <p key={c.field} className="break-all font-mono text-xs text-muted-foreground">
      {c.field.replace("protocolSection.", "")} = &quot;{c.value}&quot;
    </p>
  );
  return (
    <div className="space-y-1 rounded-md border p-2">
      <div>
        {link}
        {title && <span className="text-muted-foreground"> · {title.value}</span>}
      </div>
      {grouping.map(field)}
      {filter.length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">
            Why it matches the filters ({filter.length} fields)
          </summary>
          {filter.map(field)}
        </details>
      )}
    </div>
  );
}

function MetaPanel({ meta }: { meta: Meta }) {
  const counts = meta.counts;
  const notes = [...(meta.spec?.assumptions ?? []), ...(meta.notes ?? [])];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">How this was built</CardTitle>
        <CardDescription>
          Data as of {meta.data_timestamp ?? "unknown"} · planner {meta.models?.planner}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {counts && (
          <p className="tabular-nums">
            {counts.matched.toLocaleString()} trials matched, {counts.fetched.toLocaleString()} fetched,{" "}
            {counts.plotted.toLocaleString()} shown.
            {(counts.excluded ?? []).map((e) => (
              <span key={e.reason} className="block text-muted-foreground">
                Excluded {e.count.toLocaleString()}: {e.reason}
              </span>
            ))}
          </p>
        )}
        {meta.truncated && <Badge variant="destructive">Partial data: result was capped</Badge>}
        {(meta.queries ?? []).map((q) => (
          <p key={q.url} className="text-xs">
            <a href={q.url} target="_blank" rel="noopener noreferrer" className="text-primary underline-offset-2 hover:underline">
              {q.series ? `${q.series}: ` : ""}API query for these trials
            </a>{" "}
            <span className="text-muted-foreground">({q.matched.toLocaleString()} matched)</span>
          </p>
        ))}
        <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
