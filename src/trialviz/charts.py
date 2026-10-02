"""Builds the visualization for each chart type from loaded trials: grouping, binning, encoding.

Every datum records the trials behind it, so counts and citations come from the same set.
"""

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations, product

from trialviz.analysis import Dataset, Fact, Trial, is_estimated
from trialviz.schemas import (
    QUANTITATIVE,
    AnalysisSpec,
    Channel,
    ChartType,
    ChartVisualization,
    Citation,
    Dimension,
    Exclusion,
    NetworkData,
    NetworkEdge,
    NetworkNode,
    NetworkVisualization,
    XYEncoding,
)

MAX_BARS = 30
MAX_SERIES = 8
MAX_NODES = 40
MAX_EDGES = 150
MAX_POINTS = 5000
HISTOGRAM_BINS = 12
CITED_TRIALS = 3

TITLES = {
    Dimension.START_YEAR: "Start year",
    Dimension.PHASE: "Phase",
    Dimension.STATUS: "Overall status",
    Dimension.SPONSOR_CLASS: "Lead sponsor class",
    Dimension.LEAD_SPONSOR: "Lead sponsor",
    Dimension.INTERVENTION_TYPE: "Intervention type",
    Dimension.DRUG: "Drug",
    Dimension.CONDITION: "Condition",
    Dimension.COUNTRY: "Country",
    Dimension.ENROLLMENT: "Enrollment (participants)",
    Dimension.DURATION_MONTHS: "Duration (months)",
}
PHASE_LABELS = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not applicable",
}
SPONSOR_CLASS_LABELS = {
    "NIH": "NIH",
    "FED": "US federal",
    "OTHER_GOV": "Other government",
    "INDIV": "Individual",
    "INDUSTRY": "Industry",
    "NETWORK": "Network",
    "AMBIG": "Ambiguous",
    "OTHER": "Other",
    "UNKNOWN": "Unknown",
}
TRIAL_COUNT = Channel(field="trial_count", type="quantitative", title="Trials")

Entry = tuple[str | None, str]
"""A trial within one series: (series label, NCT ID)."""


@dataclass
class Built:
    visualization: ChartVisualization | NetworkVisualization
    plotted: int
    excluded: list[Exclusion]
    notes: list[str]


@dataclass
class Bucket:
    """The trials behind one datum, each with the evidence that put it there.

    `total` is set when the API counted the datum; `evidence` then holds sample trials only.
    """

    evidence: dict[Entry, list[Citation]] = field(default_factory=dict)
    total: int | None = None
    query: str | None = None

    def add(self, entry: Entry, evidence: Iterable[Citation]) -> None:
        if entry not in self.evidence:
            # A field can back both the grouping and a filter (e.g. start date); cite it once.
            unique = {c.field: c for c in reversed(list(evidence))}
            self.evidence[entry] = list(reversed(unique.values()))

    @property
    def nct_ids(self) -> list[str]:
        return sorted({nct_id for _, nct_id in self.evidence}, reverse=True)

    @property
    def trial_count(self) -> int:
        return self.total if self.total is not None else len(self.nct_ids)

    def support(self) -> dict:
        cited = sorted(self.evidence, key=lambda entry: entry[1], reverse=True)[:CITED_TRIALS]
        return {
            "trial_count": self.trial_count,
            "nct_ids": self.nct_ids,
            "citations": [c.model_dump() for entry in cited for c in self.evidence[entry]],
        } | ({"source_query": self.query} if self.query else {})


def build(spec: AnalysisSpec, data: Dataset) -> Built:
    return BUILDERS[spec.chart](spec, data)


def label(dimension: Dimension, value: str | int) -> str | int:
    if dimension == Dimension.PHASE:
        return PHASE_LABELS.get(str(value), str(value))
    if dimension == Dimension.SPONSOR_CLASS:
        return SPONSOR_CLASS_LABELS.get(str(value), str(value))
    if dimension in (Dimension.STATUS, Dimension.INTERVENTION_TYPE):
        return str(value).replace("_", " ").capitalize()
    return value


def _channel(dimension: Dimension) -> Channel:
    if dimension == Dimension.PHASE:
        return Channel(
            field="phase", type="ordinal", title="Phase", sort=list(PHASE_LABELS.values())
        )
    if dimension == Dimension.START_YEAR:
        return Channel(field=dimension.value, type="temporal", title=TITLES[dimension])
    kind = "quantitative" if dimension in QUANTITATIVE else "nominal"
    return Channel(field=dimension.value, type=kind, title=TITLES[dimension])


def _series_of(spec: AnalysisSpec, trial: Trial) -> list[tuple[str | None, tuple[Citation, ...]]]:
    """The series a trial belongs to: its comparison cohort, or its values of the 2nd dimension."""
    if len(spec.dimensions) < 2:
        return [(trial.series, ())]
    return [
        (str(label(spec.dimensions[1], f.value)), f.evidence)
        for f in trial.facts[spec.dimensions[1]]
    ]


def _top(totals: dict, limit: int) -> list:
    return sorted(totals, key=lambda key: (-totals[key], str(key)))[:limit]


def _buckets(
    spec: AnalysisSpec, data: Dataset, key: Callable[[str | int], str | int]
) -> tuple[dict[tuple[str | int, str | None], Bucket], set[Entry]]:
    """Group trials into (category, series) buckets for bar and line charts.

    With server counts the buckets come straight from the counts. Otherwise every fetched trial
    goes into each bucket its values put it in, and trials with no value are returned apart.
    """
    buckets: dict[tuple[str | int, str | None], Bucket] = {}
    if data.method == "count":
        for (series, combo), count in data.counts.items():
            if len(spec.dimensions) > 1:
                series = str(label(spec.dimensions[1], combo[1]))
            bucket = Bucket(total=count.total, query=count.query)
            for entry, evidence in count.evidence.items():
                bucket.add(entry, evidence)
            buckets[(key(combo[0]), series)] = bucket
        return buckets, set()

    dimension = spec.dimensions[0]
    missing: set[Entry] = set()
    for trial in data.trials:
        entry = (trial.series, trial.nct_id)
        pairs = list(product(trial.facts[dimension], _series_of(spec, trial)))
        if not pairs:
            missing.add(entry)
        for fact, (series, series_evidence) in pairs:
            buckets.setdefault((key(fact.value), series), Bucket()).add(
                entry, [*fact.evidence, *series_evidence, *trial.context]
            )
    return buckets, missing


def _accounting(
    data: Dataset,
    plotted: set[Entry],
    missing: set[Entry],
    dropped_reason: str,
    missing_reason: str = "no value for a plotted field",
) -> tuple[int, list[Exclusion]]:
    if data.method == "count":
        # The API counted every matching trial; only those with no value are known to be left out.
        excluded = [Exclusion(reason=missing_reason, count=data.missing)] if data.missing else []
        return data.matched - data.missing, excluded
    entries = {(t.series, t.nct_id) for t in data.trials}
    excluded = []
    if missing:
        excluded.append(Exclusion(reason=missing_reason, count=len(missing)))
    if dropped := entries - plotted - missing:
        excluded.append(Exclusion(reason=dropped_reason, count=len(dropped)))
    return len(plotted), excluded


MULTI_VALUED = {Dimension.PHASE, Dimension.INTERVENTION_TYPE}


def _multi_valued_note(data: Dataset, dimension: Dimension) -> list[str]:
    counted_multi = data.method == "count" and dimension in MULTI_VALUED
    if counted_multi or any(len(t.facts[dimension]) > 1 for t in data.trials):
        return [
            f"A trial can have several {TITLES[dimension].lower()} values and counts toward each."
        ]
    return []


# ------------------------------------------------------------------ bar charts


def _bars(spec: AnalysisSpec, data: Dataset) -> Built:
    dimension = spec.dimensions[0]
    buckets, missing = _buckets(spec, data, lambda value: label(dimension, value))

    totals: dict[str | int, int] = {}
    for (category, _), bucket in buckets.items():
        totals[category] = totals.get(category, 0) + bucket.trial_count
    limit = spec.top_n or MAX_BARS
    if dimension == Dimension.PHASE:
        categories = [p for p in PHASE_LABELS.values() if p in totals]
    else:
        categories = _top(totals, limit)
    series_totals: dict[str | None, int] = {}
    for (_, series), bucket in buckets.items():
        series_totals[series] = series_totals.get(series, 0) + bucket.trial_count
    kept_series = (
        set(_top(series_totals, MAX_SERIES)) if len(spec.dimensions) > 1 else set(series_totals)
    )

    rows, plotted = [], set()
    for (category, series), bucket in buckets.items():
        if category in categories and series in kept_series:
            row = {dimension.value: category} | ({"series": series} if series else {})
            rows.append(row | bucket.support())
            plotted |= bucket.evidence.keys()
    series_order = [s.label for s in spec.compare] or _top(series_totals, MAX_SERIES)
    rows.sort(
        key=lambda r: (categories.index(r[dimension.value]), series_order.index(r.get("series")))
    )

    color = None
    if spec.compare or len(spec.dimensions) > 1:
        title = TITLES[spec.dimensions[1]] if len(spec.dimensions) > 1 else "Series"
        color = Channel(field="series", type="nominal", title=title)
    count, excluded = _accounting(data, plotted, missing, f"outside the top {limit} values shown")
    return Built(
        ChartVisualization(
            type="grouped_bar" if spec.chart == ChartType.GROUPED_BAR else "bar",
            title=spec.title,
            encoding=XYEncoding(x=_channel(dimension), y=TRIAL_COUNT, color=color),
            data=rows,
        ),
        count,
        excluded,
        _multi_valued_note(data, dimension),
    )


# ----------------------------------------------------------------- time series


def _time_series(spec: AnalysisSpec, data: Dataset) -> Built:
    this_year = date.today().year
    buckets, missing = _buckets(spec, data, int)
    estimated = {
        (t.series, t.nct_id)
        for t in data.trials
        if any(is_estimated(f) for f in t.facts[Dimension.START_YEAR])
    }

    series_totals: dict[str | None, int] = {}
    for (_, series), bucket in buckets.items():
        series_totals[series] = series_totals.get(series, 0) + bucket.trial_count
    kept_series = _top(series_totals, MAX_SERIES)
    years = [
        int(y) for (y, series), b in buckets.items() if series in kept_series and b.trial_count
    ]
    years = years or [this_year]
    first, last = min(years), max(years)

    rows, plotted = [], set()
    for series in kept_series:
        for year in range(first, last + 1):
            bucket = buckets.get((year, series), Bucket())
            row = {"start_year": year} | ({"series": series} if series else {}) | bucket.support()
            if year == this_year:
                row["partial_period"] = True
            elif year > this_year:
                row["projected"] = True
            rows.append(row)
            plotted |= bucket.evidence.keys()

    notes = []
    if shown_estimated := estimated & plotted:
        notes.append(
            f"{len(shown_estimated):,} plotted trials have estimated (planned) start dates."
        )
    if first <= this_year <= last:
        notes.append(f"{this_year} is not over yet.")
    color = None
    if len(kept_series) > 1 or kept_series != [None]:
        title = TITLES[spec.dimensions[1]] if len(spec.dimensions) > 1 else "Series"
        color = Channel(field="series", type="nominal", title=title)
    count, excluded = _accounting(data, plotted, missing, "outside the series or years shown")
    return Built(
        ChartVisualization(
            type="time_series",
            title=spec.title,
            encoding=XYEncoding(x=_channel(Dimension.START_YEAR), y=TRIAL_COUNT, color=color),
            data=rows,
        ),
        count,
        excluded,
        notes,
    )


# ------------------------------------------------------------------- histogram


def _nice_step(raw: float) -> int:
    magnitude = 10 ** math.floor(math.log10(raw)) if raw >= 1 else 1
    return next(int(m * magnitude) for m in (1, 2, 5, 10) if m * magnitude >= raw)


def _histogram(spec: AnalysisSpec, data: Dataset) -> Built:
    dimension = spec.dimensions[0]
    values: list[tuple[Entry, Fact, list[Citation]]] = []
    missing: set[Entry] = set()
    for trial in data.trials:
        entry = (trial.series, trial.nct_id)
        if facts := trial.facts[dimension]:
            values.append((entry, facts[0], trial.context))
        else:
            missing.add(entry)

    ordered = sorted(int(fact.value) for _, fact, _ in values) or [0]
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    step = _nice_step(max(p95, 1) / HISTOGRAM_BINS)
    bins = math.ceil((p95 + 1) / step)
    buckets = [Bucket() for _ in range(bins + 1)]
    for entry, fact, context in values:
        buckets[min(int(fact.value) // step, bins)].add(entry, [*fact.evidence, *context])

    rows = [
        {"bin_start": i * step, "bin_end": (i + 1) * step} | bucket.support()
        for i, bucket in enumerate(buckets[:bins])
    ]
    notes = []
    if overflow := buckets[bins].evidence:
        rows.append({"bin_start": bins * step, "bin_end": ordered[-1]} | buckets[bins].support())
        notes.append(
            f"The last bin collects all {len(overflow):,} trials at or above {bins * step:,}."
        )
    plotted = {entry for entry, _, _ in values}
    count, excluded = _accounting(data, plotted, missing, "")
    x = _channel(dimension)
    return Built(
        ChartVisualization(
            type="histogram",
            title=spec.title,
            encoding=XYEncoding(
                x=x.model_copy(update={"field": "bin_start"}),
                x2=x.model_copy(update={"field": "bin_end"}),
                y=TRIAL_COUNT,
            ),
            data=rows,
        ),
        count,
        excluded,
        notes,
    )


# --------------------------------------------------------------------- scatter


def _scatter(spec: AnalysisSpec, data: Dataset) -> Built:
    x_dim, y_dim = spec.dimensions
    rows, plotted, missing = [], set(), set()
    for trial in data.trials:
        entry = (trial.series, trial.nct_id)
        xs, ys = trial.facts[x_dim], trial.facts[y_dim]
        if not (xs and ys):
            missing.add(entry)
            continue
        if len(rows) >= MAX_POINTS:
            continue
        evidence = [*xs[0].evidence, *ys[0].evidence, *trial.context]
        rows.append(
            {"nct_id": trial.nct_id, x_dim.value: xs[0].value, y_dim.value: ys[0].value}
            | {"nct_ids": [trial.nct_id], "citations": [c.model_dump() for c in evidence]}
        )
        plotted.add(entry)
    count, excluded = _accounting(data, plotted, missing, f"beyond the {MAX_POINTS:,}-point limit")
    return Built(
        ChartVisualization(
            type="scatter",
            title=spec.title,
            encoding=XYEncoding(x=_channel(x_dim), y=_channel(y_dim)),
            data=rows,
        ),
        count,
        excluded,
        [],
    )


# --------------------------------------------------------------------- network


def _network(spec: AnalysisSpec, data: Dataset) -> Built:
    def node_id(dimension: Dimension, fact: Fact) -> str:
        return f"{dimension.value}:{fact.value}"

    edges: dict[tuple[str, str], Bucket] = {}
    nodes: dict[str, tuple[Dimension, str]] = {}
    node_trials: dict[str, Bucket] = {}
    missing: set[Entry] = set()
    for trial in data.trials:
        entry = (trial.series, trial.nct_id)
        if len(spec.dimensions) == 2:
            a_dim, b_dim = spec.dimensions
            pairs = [
                ((a_dim, a), (b_dim, b)) for a, b in product(trial.facts[a_dim], trial.facts[b_dim])
            ]
        else:
            dim = spec.dimensions[0]
            values = sorted(trial.facts[dim], key=lambda f: str(f.value))
            pairs = [((dim, a), (dim, b)) for a, b in combinations(values, 2)]
        if not pairs:
            missing.add(entry)
        for (a_dim, a), (b_dim, b) in pairs:
            a_id, b_id = node_id(a_dim, a), node_id(b_dim, b)
            edges.setdefault((a_id, b_id), Bucket()).add(
                entry, [*a.evidence, *b.evidence, *trial.context]
            )
            for dim, fact, nid in ((a_dim, a, a_id), (b_dim, b, b_id)):
                nodes[nid] = (dim, str(fact.value))
                node_trials.setdefault(nid, Bucket()).add(entry, [*fact.evidence, *trial.context])

    kept_nodes = set(
        _top({n: b.trial_count for n, b in node_trials.items()}, spec.top_n or MAX_NODES)
    )
    kept_edges = _top(
        {key: len(b.evidence) for key, b in edges.items() if set(key) <= kept_nodes}, MAX_EDGES
    )
    shown = {nid for key in kept_edges for nid in key}
    plotted = {entry for key in kept_edges for entry in edges[key].evidence}

    network = NetworkData(
        nodes=[
            NetworkNode(
                id=nid, label=nodes[nid][1], group=nodes[nid][0], **node_trials[nid].support()
            )
            for nid in sorted(shown, key=lambda n: -node_trials[n].trial_count)
        ],
        edges=[NetworkEdge(source=a, target=b, **edges[(a, b)].support()) for a, b in kept_edges],
    )
    notes = []
    if len(node_trials) > len(shown):
        notes.append(
            f"Showing {len(shown)} of {len(node_trials)} connected nodes, the most frequent."
        )
    count, excluded = _accounting(
        data,
        plotted,
        missing,
        "only linked through nodes not shown",
        missing_reason="nothing to link (needs a value at both ends of an edge)",
    )
    return Built(
        NetworkVisualization(type="network", title=spec.title, data=network), count, excluded, notes
    )


BUILDERS: dict[ChartType, Callable[[AnalysisSpec, Dataset], Built]] = {
    ChartType.BAR: _bars,
    ChartType.GROUPED_BAR: _bars,
    ChartType.TIME_SERIES: _time_series,
    ChartType.HISTOGRAM: _histogram,
    ChartType.SCATTER: _scatter,
    ChartType.NETWORK: _network,
}
