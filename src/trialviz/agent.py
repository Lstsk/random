"""The planner agent: one probe tool, and two ways to finish (run_analysis or cannot_answer).

The model decides what the question means and writes an AnalysisSpec. Code runs the plan,
and any problem (a broken chart rule, a dropped request filter, no matching trials) goes back
to the model as an error it can fix.
"""

from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput, UsageLimits
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models import Model

from trialviz import analysis, charts
from trialviz.analysis import Canonicalizer, Dataset, NoTrialsError
from trialviz.charts import Built
from trialviz.config import PLANNER_SETTINGS
from trialviz.ctgov import CTGovClient, CTGovError
from trialviz.schemas import (
    AnalysisSpec,
    CannotAnswer,
    Counts,
    Filters,
    Meta,
    VisualizeRequest,
    VisualizeResponse,
)

# Probe, plan, and room for two corrections.
MAX_MODEL_REQUESTS = 6

INSTRUCTIONS = """\
You answer questions about clinical trials by planning one analysis of ClinicalTrials.gov
registry data. Code runs your plan and draws the chart; you never supply data or counts.

1. Read the question and the request filters. Request filters are fixed: copy every one into
   the plan's shared filters unchanged.
2. When a drug, condition, sponsor or country name might not match the registry, check it with
   count_trials. Zero means fix the name or loosen the filter. Probe at most twice.
3. Finish with run_analysis, or with cannot_answer when registry data cannot answer the question
   (efficacy, survival, safety outcomes, prices, anything that is not about trial registrations)
   or when required information is missing (e.g. "this drug" with no drug named).

Planning:
- Pick the dimensions and the chart that answer the question; the chart field describes which
  charts fit which dimensions.
- "Over time" or "per year" means trial start year.
- Comparing named cohorts (two drugs, two conditions) uses `compare`, one series per cohort.
- Use top_n for "most common" or "top" questions.
- The title describes what is shown; do not state counts or other results in it.
- List every interpretation choice a reader should know in `assumptions`.
"""


@dataclass
class Deps:
    request: VisualizeRequest
    client: CTGovClient
    labeler: Canonicalizer


@dataclass
class Answer:
    spec: AnalysisSpec
    data: Dataset
    built: Built


def run_analysis(ctx: RunContext[Deps], spec: AnalysisSpec) -> Answer:
    """Run the plan against ClinicalTrials.gov and build the chart. This ends your turn."""
    if dropped := dropped_request_filters(ctx.deps.request, spec):
        raise ModelRetry(f"Keep the request filters unchanged in every series: {dropped}.")
    try:
        data = analysis.load(spec, ctx.deps.client, ctx.deps.labeler)
    except NoTrialsError as exc:
        raise ModelRetry(f"{exc} Check names with count_trials or loosen a filter.") from exc
    built = charts.build(spec, data)
    if built.plotted == 0:
        raise ModelRetry(
            f"{len(data.trials)} trials matched, but none has a value for "
            f"{[d.value for d in spec.dimensions]}. Choose other dimensions."
        )
    return Answer(spec, data, built)


OUTPUTS = [
    ToolOutput(run_analysis, name="run_analysis"),
    ToolOutput(CannotAnswer, name="cannot_answer"),
]

# ty (beta) cannot match Agent's overloads for a list of output tools.
agent: Agent[Deps, Answer | CannotAnswer] = Agent(  # ty: ignore[invalid-assignment, no-matching-overload]
    deps_type=Deps,
    instructions=INSTRUCTIONS,
    output_type=OUTPUTS,
    retries={"tools": 1, "output": 2},
    defer_model_check=True,
)


@agent.tool
def count_trials(ctx: RunContext[Deps], filters: Filters) -> int:
    """Count registry trials matching the filters, to check names before planning."""
    return ctx.deps.client.count(filters)


def dropped_request_filters(request: VisualizeRequest, spec: AnalysisSpec) -> list[str]:
    """Request filters that some series of the plan drops or changes."""
    required = Filters.model_validate(request.model_dump(exclude={"query"}))
    wanted = required.model_dump(exclude_defaults=True)
    dropped = set()
    for _, effective in analysis.series_filters(spec):
        actual = effective.model_dump()
        for name, value in wanted.items():
            if _normalized(actual[name]) != _normalized(value):
                dropped.add(name)
    return sorted(dropped)


def _normalized(value: object) -> object:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, list):
        return sorted(value)
    return value


def visualize(
    request: VisualizeRequest, client: CTGovClient, labeler: Canonicalizer, model: Model | str
) -> VisualizeResponse:
    planner = model if isinstance(model, str) else model.model_name
    models = {"planner": planner, "labeler": getattr(labeler, "model", "unknown")}
    filters = request.model_dump(exclude={"query"}, exclude_defaults=True)
    prompt = f"Question: {request.query}\nRequest filters (fixed): {filters or 'none'}"
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
    meta.truncated = data.truncated
    meta.notes = data.notes + built.notes
    meta.counts = Counts(
        matched=data.matched, fetched=data.fetched, plotted=built.plotted, excluded=built.excluded
    )
    return VisualizeResponse(status="ok", visualization=built.visualization, meta=meta)
