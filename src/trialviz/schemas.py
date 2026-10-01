"""Service contract: the request, the agent's analysis plan, and the response.

FastAPI publishes these models as OpenAPI at /docs. The agent receives AnalysisSpec as its
tool schema, so the validators here are also the rules the model must satisfy; a violation is
returned to the model as an error it can fix.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ClinicalTrials.gov enum values, copied from GET /api/v2/studies/enums.


class Phase(StrEnum):
    NA = "NA"
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"


class Status(StrEnum):
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------- request


class Filters(BaseModel):
    """Which trials to include. Every field is optional; set fields are ANDed together."""

    model_config = ConfigDict(extra="forbid")

    drug_name: str | None = Field(
        None,
        max_length=100,
        description="Drug or biologic listed as a trial intervention. Synonyms known to "
        "ClinicalTrials.gov match too (Keytruda and MK-3475 both match pembrolizumab).",
    )
    condition: str | None = Field(None, max_length=100, description="Disease or condition studied.")
    sponsor: str | None = Field(None, max_length=100, description="Lead or collaborating sponsor.")
    country: str | None = Field(
        None, max_length=60, description="Country with at least one trial site."
    )
    phases: list[Phase] = Field(default_factory=list, description="Keep trials in any of these.")
    statuses: list[Status] = Field(
        default_factory=list, description="Keep trials whose overall status is any of these."
    )
    start_year: int | None = Field(
        None, ge=1900, le=2100, description="Earliest trial start year, inclusive."
    )
    end_year: int | None = Field(
        None, ge=1900, le=2100, description="Latest trial start year, inclusive."
    )

    @model_validator(mode="after")
    def _years_in_order(self) -> "Filters":
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must not be after end_year")
        return self


class VisualizeRequest(Filters):
    """Body of POST /visualize: a question plus optional filters.

    Filters given here are hard constraints. The agent may add filters it reads from the
    question, but it cannot drop or change these.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "How has the number of trials for this drug changed over time?",
                    "drug_name": "Pembrolizumab",
                },
                {"query": "Show a network of sponsors and drugs for glioblastoma trials"},
            ]
        },
    )

    query: str = Field(
        min_length=3, max_length=500, description="Natural-language question about clinical trials."
    )


# ---------------------------------------------------------- analysis plan


class Dimension(StrEnum):
    """Trial attributes the analysis can group, bin or plot by."""

    START_YEAR = "start_year"
    PHASE = "phase"
    STATUS = "status"
    SPONSOR_CLASS = "sponsor_class"
    LEAD_SPONSOR = "lead_sponsor"
    INTERVENTION_TYPE = "intervention_type"
    DRUG = "drug"
    CONDITION = "condition"
    COUNTRY = "country"
    ENROLLMENT = "enrollment"
    DURATION_MONTHS = "duration_months"


TEMPORAL = {Dimension.START_YEAR}
QUANTITATIVE = {Dimension.ENROLLMENT, Dimension.DURATION_MONTHS}
CATEGORICAL = set(Dimension) - TEMPORAL - QUANTITATIVE
ENTITIES = {Dimension.LEAD_SPONSOR, Dimension.DRUG, Dimension.CONDITION, Dimension.COUNTRY}


def _names(dimensions: set[Dimension]) -> str:
    return ", ".join(sorted(d.value for d in dimensions))


class ChartType(StrEnum):
    BAR = "bar"
    GROUPED_BAR = "grouped_bar"
    TIME_SERIES = "time_series"
    HISTOGRAM = "histogram"
    SCATTER = "scatter"
    NETWORK = "network"


CHART_RULES = {
    ChartType.BAR: "one categorical dimension",
    ChartType.GROUPED_BAR: "one categorical dimension plus a comparison or a second "
    "categorical dimension",
    ChartType.TIME_SERIES: "start_year first, optionally a categorical dimension or a "
    "comparison as the series",
    ChartType.HISTOGRAM: "one quantitative dimension",
    ChartType.SCATTER: "two different quantitative dimensions, one point per trial",
    ChartType.NETWORK: "one entity dimension (co-occurrence within a trial, e.g. drug-drug) or "
    "two different entity dimensions (bipartite, e.g. sponsor-drug)",
}


class Series(BaseModel):
    """One cohort in a comparison, e.g. the trials for one of two drugs."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(max_length=60, description="Legend label for this cohort.")
    filters: Filters = Field(description="Added on top of the plan's shared filters.")


class AnalysisSpec(BaseModel):
    """The agent's plan for answering a question. Code executes it; the model supplies no data."""

    model_config = ConfigDict(extra="forbid")

    filters: Filters = Field(
        default_factory=Filters,
        description="Shared by every series. Must include every filter from the request.",
    )
    compare: list[Series] = Field(
        default_factory=list,
        max_length=4,
        description="Two to four cohorts to compare side by side; empty for no comparison.",
    )
    dimensions: list[Dimension] = Field(
        min_length=1,
        max_length=2,
        description=f"Temporal: {_names(TEMPORAL)}. Quantitative: {_names(QUANTITATIVE)}. "
        f"Entities: {_names(ENTITIES)}. All others are categorical.",
    )
    chart: ChartType = Field(
        description="Must fit the dimensions. "
        + " ".join(f"{chart.value}: {rule}." for chart, rule in CHART_RULES.items())
    )
    top_n: int | None = Field(
        None,
        ge=3,
        le=50,
        description="Keep only the N largest categories, or the N best-connected network nodes.",
    )
    title: str = Field(
        max_length=120, description="What the chart shows. Do not state counts or results."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="Interpretation choices a reader should know, e.g. 'start year, not "
        "completion year'.",
    )

    @model_validator(mode="after")
    def _chart_fits_dimensions(self) -> "AnalysisSpec":
        dims, chart = self.dimensions, self.chart
        first, second = dims[0], dims[1] if len(dims) > 1 else None
        comparing = bool(self.compare)

        if len(self.compare) == 1:
            raise ValueError("compare needs at least two series, or none")
        if comparing and chart not in (ChartType.GROUPED_BAR, ChartType.TIME_SERIES):
            raise ValueError("comparisons are drawn as grouped_bar or time_series")

        match chart:
            case ChartType.BAR:
                ok = second is None and first in CATEGORICAL
            case ChartType.GROUPED_BAR:
                ok = first in CATEGORICAL and (
                    (comparing and second is None) or (not comparing and second in CATEGORICAL)
                )
            case ChartType.TIME_SERIES:
                ok = first in TEMPORAL and (second is None or second in CATEGORICAL)
                ok = ok and not (comparing and second)
            case ChartType.HISTOGRAM:
                ok = second is None and first in QUANTITATIVE
            case ChartType.SCATTER:
                ok = first in QUANTITATIVE and second in QUANTITATIVE and first != second
            case ChartType.NETWORK:
                ok = first in ENTITIES and (second is None or second in ENTITIES)
                ok = ok and first != second

        if not ok:
            raise ValueError(
                f"chart {chart.value!r} does not fit dimensions {[d.value for d in dims]}"
                f"{' with a comparison' if comparing else ''}; {chart.value} needs "
                f"{CHART_RULES[chart]}"
            )
        return self


class CannotAnswer(BaseModel):
    """The agent's way out when the question can't be answered from trial registry data."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=300)
    suggestion: str | None = Field(
        None, max_length=300, description="A related question that can be answered."
    )


# --------------------------------------------------------------- response


class Citation(BaseModel):
    """One API field value that puts a trial into a datum."""

    nct_id: str = Field(examples=["NCT01876511"])
    field: str = Field(
        description="Path in the ClinicalTrials.gov study record.",
        examples=["protocolSection.armsInterventionsModule.interventions[0].name"],
    )
    value: str = Field(description="Exact value at that path.", examples=["MK-3475"])


class Channel(BaseModel):
    """Maps a data field to a visual channel, in the style of Vega-Lite."""

    field: str
    type: Literal["nominal", "ordinal", "quantitative", "temporal"]
    title: str
    sort: list[str] | None = Field(
        None, description="Explicit category order for ordinal fields, e.g. phases."
    )


class XYEncoding(BaseModel):
    x: Channel
    x2: Channel | None = Field(None, description="Bin end, for histograms.")
    y: Channel
    color: Channel | None = Field(None, description="Series, for grouped bars and multi-line.")


class ChartVisualization(BaseModel):
    """Bar, grouped bar, time series, histogram or scatter.

    `data` is a flat list of records; `encoding` names the keys to plot. Besides those keys,
    every record carries:
    - `nct_ids`: the trials counted in the record (one trial for scatter points); when
      `meta.counts.method` is `server_counts` this is a sample and `trial_count` is the total,
    - `citations`: the field values that put up to three of those trials there.
    Time series records may also carry `partial_period: true` (the year is not over) or
    `projected: true` (the year is in the future, so its trials have planned start dates).
    """

    type: Literal["bar", "grouped_bar", "time_series", "histogram", "scatter"]
    title: str
    encoding: XYEncoding
    data: list[dict[str, Any]]


class NetworkNode(BaseModel):
    id: str
    label: str
    group: Dimension = Field(description="Entity kind; colour nodes by this.")
    trial_count: int = Field(description="Distinct trials touching this node; size by this.")
    nct_ids: list[str]


class NetworkEdge(BaseModel):
    source: str
    target: str
    trial_count: int = Field(description="Distinct trials linking both ends; width by this.")
    nct_ids: list[str]
    citations: list[Citation] = Field(
        description="Field values naming both ends, for up to three of the trials."
    )


class NetworkData(BaseModel):
    nodes: list[NetworkNode]
    edges: list[NetworkEdge]


class NetworkEncoding(BaseModel):
    """Fixed mapping from node and edge fields to visual channels."""

    node_label: Literal["label"] = "label"
    node_size: Literal["trial_count"] = "trial_count"
    node_color: Literal["group"] = "group"
    edge_width: Literal["trial_count"] = "trial_count"


class NetworkVisualization(BaseModel):
    """Node-link graph: bipartite when nodes come from two groups, co-occurrence when one."""

    type: Literal["network"]
    title: str
    encoding: NetworkEncoding = Field(default_factory=NetworkEncoding)
    data: NetworkData


Visualization = Annotated[ChartVisualization | NetworkVisualization, Field(discriminator="type")]


class Exclusion(BaseModel):
    reason: str
    count: int


class Counts(BaseModel):
    """How the numbers were obtained, and what was left out.

    - fetched_records: trials were downloaded and counted here; fetched = plotted +
      sum(excluded counts). With a comparison, a trial in two series counts once in each.
    - server_counts: ClinicalTrials.gov counted each value directly (used for large result sets
      or when it needs fewer requests); counts cover every matching trial, and each datum's
      nct_ids are a sample.
    """

    method: Literal["fetched_records", "server_counts"] = "fetched_records"
    matched: int = Field(description="Trials the API matched for the filters.")
    fetched: int = Field(description="Trials downloaded; lower than matched when truncated.")
    plotted: int = Field(description="Trials that appear in the visualization.")
    excluded: list[Exclusion] = Field(default_factory=list)


class Meta(BaseModel):
    source: str = "ClinicalTrials.gov API v2"
    data_timestamp: str | None = Field(None, description="When ClinicalTrials.gov last refreshed.")
    spec: AnalysisSpec | None = Field(None, description="The plan that was executed.")
    counts: Counts | None = None
    truncated: bool = Field(False, description="True when only the first trials were fetched.")
    notes: list[str] = Field(
        default_factory=list,
        description="Data caveats found while building the chart, e.g. estimated start dates.",
    )
    models: dict[str, str] = Field(
        default_factory=dict, description="Model used for each role, e.g. planner and labeler."
    )
    usage: dict[str, int] = Field(
        default_factory=dict, description="Planner model requests, tool calls and tokens."
    )


class VisualizeResponse(BaseModel):
    """Body returned by POST /visualize.

    `status` decides what is present:
    - ok: `visualization` and `meta.spec` are set.
    - cannot_answer: `message` says why; `visualization` is null.
    - error: `message` describes the failure; `visualization` is null.
    """

    status: Literal["ok", "cannot_answer", "error"]
    message: str | None = None
    visualization: Visualization | None = None
    meta: Meta = Field(default_factory=Meta)
