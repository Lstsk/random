"""The planner agent. It works the ClinicalTrials.gov API directly through tools, then finishes
with a plan (run_analysis) or a refusal (cannot_answer).

The model explores and decides: it writes API queries, looks at what the registry contains,
previews its chart and corrects it. Code executes: it runs queries, counts, builds the chart
and cites sources, so every number comes from the API. The request's own filters are added to
every query by code, so the model cannot drop them.
"""

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput, UsageLimits
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models import Model

from trialviz import analysis, charts
from trialviz.analysis import Canonicalizer, Dataset, NoTrialsError, series_queries, values_at
from trialviz.charts import Built
from trialviz.config import PLANNER_SETTINGS
from trialviz.ctgov import CTGovClient, CTGovError, and_queries, request_query
from trialviz.schemas import (
    AnalysisSpec,
    CannotAnswer,
    Counts,
    Meta,
    Query,
    VisualizeRequest,
    VisualizeResponse,
)

# Room to explore, preview and correct; most questions need three to five.
MAX_MODEL_REQUESTS = 12
COUNT_BY_MAX_RECORDS = 3000
DEFAULT_FIELDS = ["BriefTitle", "Phase", "OverallStatus", "Condition", "InterventionName"]

SKILL = (Path(__file__).parent / "skills" / "ctgov_api.md").read_text()

INSTRUCTIONS = f"""\
You answer questions about clinical trials with a chart of ClinicalTrials.gov registry data.
You work the registry's API through tools; code computes every number, so never state counts
you have not seen in a tool result.

How to work:
1. Decide what the question asks. If registry data cannot answer it (efficacy, survival, safety
   outcomes, prices, anything that is not about trial registrations) or it refers to something
   that was not given (e.g. "this drug" with no drug named), call cannot_answer.
2. Explore with search_studies and count_by to learn how the registry records what you need:
   which drug names appear, how a condition is written, which query catches the right trials.
   Prefer what you see in the data over what you remember. Skip this for simple questions.
3. Write the plan and call preview. Check the rows: do they answer the question? Is the top of
   the chart what was asked about, or noise such as background chemotherapy? Fix the plan
   (query, keep_values / drop_values, dimensions) and preview again if needed.
4. Finish with run_analysis using the plan you previewed.

Planning rules:
- The request's filters are added to every query for you; do not repeat them.
- "Over time" or "per year" means trial start year.
- Comparing named cohorts (two drugs, two conditions) uses `compare`, one series per cohort.
- Use top_n for "most common" or "top" questions.
- The title describes what is shown, without counts or other results.
- List every interpretation choice in `assumptions`, including any list of names you chose
  (e.g. which drugs you counted as PD-1 inhibitors). State choices, never facts about the data.

{SKILL}"""


@dataclass
class Deps:
    request: VisualizeRequest
    client: CTGovClient
    labeler: Canonicalizer

    @property
    def required(self) -> Query:
        return request_query(self.request)


@dataclass
class Answer:
    spec: AnalysisSpec
    data: Dataset
    built: Built


def execute(deps: Deps, spec: AnalysisSpec) -> Answer:
    """Run a plan; problems become retry messages the model can act on."""
    try:
        if any(q.is_empty() for _, q in series_queries(spec, deps.required)):
            raise ModelRetry("A series has an empty query, which would cover the whole registry.")
        data = analysis.load(spec, deps.client, deps.labeler, deps.required)
    except NoTrialsError as exc:
        raise ModelRetry(f"{exc} Explore with search_studies to fix the query.") from exc
    except (CTGovError, ValueError) as exc:
        raise ModelRetry(f"The query was rejected: {exc}") from exc
    built = charts.build(spec, data)
    if built.plotted == 0:
        raise ModelRetry(
            f"{data.matched} trials matched, but none has a plottable value for "
            f"{[d.value for d in spec.dimensions]} within keep_values / drop_values."
        )
    return Answer(spec, data, built)


def run_analysis(ctx: RunContext[Deps], spec: AnalysisSpec) -> Answer:
    """Run the plan and return the chart. This ends your turn."""
    return execute(ctx.deps, spec)


OUTPUTS = [
    ToolOutput(run_analysis, name="run_analysis"),
    ToolOutput(CannotAnswer, name="cannot_answer"),
]

# ty (beta) cannot match Agent's overloads for a list of output tools.
agent: Agent[Deps, Answer | CannotAnswer] = Agent(  # ty: ignore[invalid-assignment, no-matching-overload]
    deps_type=Deps,
    instructions=INSTRUCTIONS,
    output_type=OUTPUTS,
    retries={"tools": 3, "output": 2},
    defer_model_check=True,
)


@agent.tool
def search_studies(
    ctx: RunContext[Deps], query: Query, fields: list[str] | None = None, limit: int = 5
) -> dict[str, Any]:
    """Run a ClinicalTrials.gov search (GET /studies). Returns the total match count and up to
    `limit` (max 20) studies showing `fields` (API field names such as InterventionName)."""
    fields = fields or DEFAULT_FIELDS
    paths = _known_fields(ctx.deps.client, fields)
    try:
        query = and_queries(ctx.deps.required, query)
        result = ctx.deps.client.sample(query, fields, max(1, min(limit, 20)))
    except (CTGovError, ValueError) as exc:
        raise ModelRetry(f"The query was rejected: {exc}") from exc
    studies = []
    for study in result.studies:
        row: dict[str, Any] = {"NCTId": study["protocolSection"]["identificationModule"]["nctId"]}
        for name in fields:
            values = [_short(v) for _, v in values_at(study, paths[name])]
            row[name] = values[0] if len(values) == 1 else values
        studies.append(row)
    return {"total": result.total, "studies": studies}


@agent.tool
def count_by(ctx: RunContext[Deps], query: Query, field: str, top: int = 20) -> dict[str, Any]:
    """Count matching trials per value of one field (e.g. InterventionName, Phase,
    LeadSponsorName), most common first, to see what the data actually contains. Counts the
    first 3,000 matching trials when there are more."""
    paths = _known_fields(ctx.deps.client, [field])
    try:
        query = and_queries(ctx.deps.required, query)
        result = ctx.deps.client.search(query, [field], COUNT_BY_MAX_RECORDS)
    except (CTGovError, ValueError) as exc:
        raise ModelRetry(f"The query was rejected: {exc}") from exc
    counts: dict[str, int] = {}
    for study in result.studies:
        for value in {str(v) for _, v in values_at(study, paths[field])}:
            counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[: max(1, min(top, 50))]
    return {
        "total": result.total,
        "counted": len(result.studies),
        "values": [{"value": _short(v), "trials": n} for v, n in ranked],
    }


@agent.tool
def preview(ctx: RunContext[Deps], spec: AnalysisSpec) -> dict[str, Any]:
    """Run a plan without finishing and summarise the chart it would draw, so you can check it
    answers the question before calling run_analysis."""
    answer = execute(ctx.deps, spec)
    viz, data = answer.built.visualization, answer.data
    if viz.type == "network":
        rows = [f"{e.source} -- {e.target}: {e.trial_count}" for e in viz.data.edges[:15]]
    else:
        keys = [viz.encoding.x.field, *([viz.encoding.color.field] if viz.encoding.color else [])]
        rows = [
            " / ".join(str(r.get(k)) for k in keys) + f": {r.get('trial_count', 1)}"
            for r in viz.data[:15]
        ]
    return {
        "matched": data.matched,
        "plotted": answer.built.plotted,
        "excluded": [f"{e.count} {e.reason}" for e in answer.built.excluded],
        "rows": rows,
        "notes": data.notes + answer.built.notes,
    }


def _known_fields(client: CTGovClient, fields: list[str]) -> dict[str, str]:
    paths = client.field_paths()
    for name in fields:
        if name not in paths:
            close = difflib.get_close_matches(name, paths, n=3)
            raise ModelRetry(f"Unknown field {name!r}. Close matches: {close}.")
    return paths


def _short(value: object, limit: int = 160) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def visualize(
    request: VisualizeRequest, client: CTGovClient, labeler: Canonicalizer, model: Model | str
) -> VisualizeResponse:
    planner = model if isinstance(model, str) else model.model_name
    models = {"planner": planner, "labeler": getattr(labeler, "model", "unknown")}
    filters = request.model_dump(exclude={"query"}, exclude_defaults=True)
    prompt = f"Question: {request.query}\nRequest filters (applied for you): {filters or 'none'}"
    try:
        result = agent.run_sync(
            prompt,
            model=model,
            deps=Deps(request, client, labeler),
            model_settings=PLANNER_SETTINGS,
            usage_limits=UsageLimits(request_limit=MAX_MODEL_REQUESTS),
        )
    except (AgentRunError, CTGovError) as exc:
        return VisualizeResponse(status="error", message=str(exc), meta=Meta(models=models))

    usage = result.usage
    meta = Meta(
        models=models,
        usage={
            "model_requests": usage.requests,
            "tool_calls": usage.tool_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
    )
    output = result.output
    if isinstance(output, CannotAnswer):
        message = output.reason + (
            f" Try instead: {output.suggestion}" if output.suggestion else ""
        )
        return VisualizeResponse(status="cannot_answer", message=message, meta=meta)

    data, built = output.data, output.built
    meta.spec = output.spec
    meta.data_timestamp = client.data_timestamp()
    meta.queries = data.queries
    meta.truncated = data.truncated
    meta.notes = data.notes + built.notes
    meta.counts = Counts(
        method="server_counts" if data.method == "count" else "fetched_records",
        matched=data.matched,
        fetched=data.fetched,
        plotted=built.plotted,
        excluded=built.excluded,
    )
    return VisualizeResponse(status="ok", visualization=built.visualization, meta=meta)
