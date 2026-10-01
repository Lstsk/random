"use client";

import { useState } from "react";

import { QueryForm } from "@/components/query-form";
import { ResultView } from "@/components/result-view";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { visualize, type VisualizeRequest, type VisualizeResponse } from "@/lib/api";

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "done"; response: VisualizeResponse; seconds: number }
  | { kind: "failed"; message: string };

export default function Home() {
  const [state, setState] = useState<State>({ kind: "idle" });

  async function ask(request: VisualizeRequest) {
    setState({ kind: "loading" });
    const started = performance.now();
    try {
      const response = await visualize(request);
      setState({ kind: "done", response, seconds: (performance.now() - started) / 1000 });
    } catch (error) {
      setState({ kind: "failed", message: error instanceof Error ? error.message : String(error) });
    }
  }

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-8">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Trial Visualizer</h1>
        <p className="text-muted-foreground">
          Ask a question about clinical trials. Charts are built from ClinicalTrials.gov data, and every bar, point,
          node and link lists the trials behind it.
        </p>
      </header>

      <Card>
        <CardContent>
          <QueryForm busy={state.kind === "loading"} onSubmit={ask} />
        </CardContent>
      </Card>

      {state.kind === "loading" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Planning the analysis and fetching trials. This usually takes 5 to 40 seconds.
          </p>
          <Skeleton className="h-[360px] w-full" />
        </div>
      )}
      {state.kind === "failed" && (
        <Alert variant="destructive">
          <AlertTitle>Request failed</AlertTitle>
          <AlertDescription>{state.message}</AlertDescription>
        </Alert>
      )}
      {state.kind === "done" && state.response.status !== "ok" && (
        <Alert variant={state.response.status === "error" ? "destructive" : "default"}>
          <AlertTitle>{state.response.status === "error" ? "Something went wrong" : "Can't answer that from trial registry data"}</AlertTitle>
          <AlertDescription>{state.response.message}</AlertDescription>
        </Alert>
      )}
      {state.kind === "done" && state.response.status === "ok" && (
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">Done in {state.seconds.toFixed(1)} s.</p>
          <ResultView key={state.response.visualization?.title} response={state.response} />
        </div>
      )}
    </main>
  );
}
