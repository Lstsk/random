// Types come from the backend's OpenAPI schema: `npm run gen:api` regenerates api-schema.ts.
import type { components } from "./api-schema";

type Schemas = components["schemas"];
export type VisualizeRequest = Schemas["VisualizeRequest"];
export type VisualizeResponse = Schemas["VisualizeResponse"];
export type ChartVisualization = Schemas["ChartVisualization"];
export type NetworkVisualization = Schemas["NetworkVisualization"];
export type Citation = Schemas["Citation"];
export type Meta = Schemas["Meta"];

/** A data record from ChartVisualization.data: encoded fields plus nct_ids and citations. */
export type Datum = Record<string, unknown> & { nct_ids?: string[]; citations?: Citation[] };

/** What the evidence panel shows: one or more data points and the trials behind them. */
export type Evidence = { label: string; datum: Datum }[];

export async function visualize(request: VisualizeRequest): Promise<VisualizeResponse> {
  const res = await fetch("/api/visualize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ? JSON.stringify(body.detail) : `HTTP ${res.status}`);
  }
  return res.json();
}
