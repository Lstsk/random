"use client";

import cytoscape from "cytoscape";
import { useEffect, useMemo, useRef } from "react";

import type { Evidence, NetworkVisualization } from "@/lib/api";

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Cytoscape cannot parse the oklch() colors shadcn uses, so the neutrals are plain hex.
const LABEL = "#3f3f46";
const EDGE = "#a1a1aa";
const OUTLINE = "#ffffff";

export function NetworkGraph({ viz, onSelect }: { viz: NetworkVisualization; onSelect: (e: Evidence) => void }) {
  const container = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => [...new Set(viz.data.nodes.map((n) => n.group))], [viz]);

  useEffect(() => {
    if (!container.current) return;
    const { nodes, edges } = viz.data;
    const maxNode = Math.max(1, ...nodes.map((n) => n.trial_count));
    const maxEdge = Math.max(1, ...edges.map((e) => e.trial_count));
    const cy = cytoscape({
      container: container.current,
      elements: [
        ...nodes.map((n) => ({
          data: { ...n, size: 14 + 36 * Math.sqrt(n.trial_count / maxNode), color: css(`--chart-${groups.indexOf(n.group) + 1}`) },
        })),
        ...edges.map((e, i) => ({ data: { ...e, id: `e${i}`, width: 1 + 7 * (e.trial_count / maxEdge) } })),
      ],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            width: "data(size)",
            height: "data(size)",
            label: "data(label)",
            "font-size": 10,
            color: LABEL,
            "text-valign": "bottom",
            "text-margin-y": 3,
            "text-wrap": "ellipsis",
            "text-max-width": "120px",
            "text-outline-color": OUTLINE,
            "text-outline-width": 2,
            "border-width": 2,
            "border-color": OUTLINE,
          },
        },
        { selector: "edge", style: { width: "data(width)", "line-color": EDGE, opacity: 0.45, "curve-style": "haystack" } },
        { selector: ":selected", style: { "border-color": LABEL, "line-color": css("--chart-1"), opacity: 1 } },
      ],
      layout: { name: "cose", animate: false, nodeRepulsion: () => 60000, idealEdgeLength: () => 160, nodeOverlap: 40, padding: 30 },
    });
    cy.on("tap", "node", (event) => {
      const d = event.target.data();
      onSelect([{ label: `${d.label} (${d.group.replace("_", " ")})`, datum: d }]);
    });
    cy.on("tap", "edge", (event) => {
      const d = event.target.data();
      const name = (id: string) => cy.getElementById(id).data("label");
      onSelect([{ label: `${name(d.source)} — ${name(d.target)}`, datum: d }]);
    });
    return () => cy.destroy();
  }, [viz, onSelect, groups]);

  return (
    <div className="space-y-2">
      <div className="flex gap-4 text-sm text-muted-foreground">
        {groups.map((g, i) => (
          <span key={g} className="flex items-center gap-1.5">
            <span className="size-2.5 rounded-full" style={{ background: `var(--chart-${i + 1})` }} />
            {g.replace("_", " ")}
          </span>
        ))}
        <span>Node size and link width show trial counts.</span>
      </div>
      <div ref={container} className="h-[560px] w-full rounded-md border" />
    </div>
  );
}
