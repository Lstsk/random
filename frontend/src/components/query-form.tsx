"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { VisualizeRequest } from "@/lib/api";

const EXAMPLES: VisualizeRequest[] = [
  { query: "How has the number of trials for this drug changed over time?", drug_name: "Pembrolizumab" },
  { query: "Compare phases for trials involving pembrolizumab vs nivolumab" },
  { query: "Which countries have the most recruiting trials for type 1 diabetes?" },
  { query: "Show a network of sponsors and drugs for glioblastoma trials" },
  { query: "Which drugs frequently co-occur in combination studies for melanoma?" },
  { query: "What is the distribution of enrollment sizes for phase 3 glioblastoma trials?" },
];

const FILTERS = [
  { name: "drug_name", label: "Drug" },
  { name: "condition", label: "Condition" },
  { name: "sponsor", label: "Sponsor" },
  { name: "country", label: "Country" },
  { name: "start_year", label: "From year", type: "number" },
  { name: "end_year", label: "To year", type: "number" },
] as const;

type Fields = Record<string, string>;

export function QueryForm({ busy, onSubmit }: { busy: boolean; onSubmit: (r: VisualizeRequest) => void }) {
  const [fields, setFields] = useState<Fields>({ query: "" });

  function submit(values: Fields) {
    const request: VisualizeRequest = { query: values.query.trim() };
    for (const f of FILTERS) {
      const value = values[f.name]?.trim();
      if (value) Object.assign(request, { [f.name]: f.name.endsWith("_year") ? Number(value) : value });
    }
    onSubmit(request);
  }

  function runExample(example: VisualizeRequest) {
    const values = Object.fromEntries(Object.entries(example).map(([k, v]) => [k, String(v)]));
    setFields(values);
    submit(values);
  }

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        submit(fields);
      }}
    >
      <div className="flex gap-2">
        <Input
          aria-label="Question"
          required
          minLength={3}
          placeholder="Ask about clinical trials, e.g. How are melanoma trials distributed across phases?"
          value={fields.query ?? ""}
          onChange={(e) => setFields({ ...fields, query: e.target.value })}
        />
        <Button type="submit" disabled={busy}>
          {busy ? "Working…" : "Visualize"}
        </Button>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {FILTERS.map((f) => (
          <div key={f.name} className="space-y-1">
            <Label htmlFor={f.name} className="text-xs text-muted-foreground">
              {f.label}
            </Label>
            <Input
              id={f.name}
              type={"type" in f ? f.type : "text"}
              placeholder="Optional"
              value={fields[f.name] ?? ""}
              onChange={(e) => setFields({ ...fields, [f.name]: e.target.value })}
            />
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <Button
            key={example.query + (example.drug_name ?? "")}
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => runExample(example)}
          >
            {example.query}
            {example.drug_name ? ` (${example.drug_name})` : ""}
          </Button>
        ))}
      </div>
    </form>
  );
}
