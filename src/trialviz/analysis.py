"""Loads the data an AnalysisSpec needs: fetch trials per series, extract dimension values.

Every extracted value keeps the exact API field and value it came from, so any datum built
from it can cite its source.
"""

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from itertools import product
from typing import Any, Literal

from trialviz.ctgov import PAGE_SIZE, CTGovClient, and_queries, with_clauses
from trialviz.schemas import (
    AnalysisSpec,
    ChartType,
    Citation,
    Dimension,
    Phase,
    Query,
    SourceQuery,
    Status,
)

MAX_RECORDS = 10_000
MAX_LABELED_NAMES = 1500
DRUG_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"}

# Free-text dimensions whose spellings vary ("TMZ", "Temodar (temozolomide)"); a model maps
# them to canonical names before grouping.
LABELED = {Dimension.DRUG, Dimension.CONDITION}

Canonicalizer = Callable[[Dimension, list[str]], dict[str, list[str]]]
"""Maps each raw name to canonical names: one for a variant, several for a combination of
drugs, none for something that is not an entity of that kind (placebo, a procedure)."""


# Dimensions with a fixed set of values, which the API can count one value at a time:
# dimension -> (search area, values). Start years are found per question.
COUNTABLE: dict[Dimension, tuple[str, list[str]]] = {
    Dimension.PHASE: ("Phase", [p.value for p in Phase]),
    Dimension.STATUS: ("OverallStatus", [s.value for s in Status]),
    Dimension.SPONSOR_CLASS: (
        "LeadSponsorClass",
        ["NIH", "FED", "OTHER_GOV", "INDIV", "INDUSTRY", "NETWORK", "AMBIG", "OTHER", "UNKNOWN"],
    ),
    Dimension.INTERVENTION_TYPE: (
        "InterventionType",
        [
            "BEHAVIORAL",
            "BIOLOGICAL",
            "COMBINATION_PRODUCT",
            "DEVICE",
            "DIAGNOSTIC_TEST",
            "DIETARY_SUPPLEMENT",
            "DRUG",
            "GENETIC",
            "PROCEDURE",
            "RADIATION",
            "OTHER",
        ],
    ),
    Dimension.START_YEAR: ("StartDate", []),
}
COUNTABLE_CHARTS = {ChartType.BAR, ChartType.GROUPED_BAR, ChartType.TIME_SERIES}
# Each count is one request at ~1 request/s, so a plan needing more is fetched instead.
MAX_COUNT_QUERIES = 80
SAMPLE_SIZE = 3


class NoTrialsError(Exception):
    pass


@dataclass(frozen=True)
class Fact:
    value: str | int
    evidence: tuple[Citation, ...]


@dataclass
class Trial:
    nct_id: str
    series: str | None
    facts: dict[Dimension, list[Fact]]
    context: list[Citation] = field(default_factory=list)
    """Citations that show the trial matches the filters, plus its title."""


@dataclass
class Count:
    """A server-side count for one combination of dimension values, with sample evidence."""

    total: int
    evidence: dict[tuple[str | None, str], list[Citation]]
    query: str
    """The API request whose totalCount is `total`."""


@dataclass
class Dataset:
    """Fetched trials ("fetch"), or per-value counts from the API ("count")."""

    trials: list[Trial]
    matched: int
    fetched: int
    truncated: bool
    notes: list[str]
    method: Literal["fetch", "count"] = "fetch"
    counts: dict[tuple[str | None, tuple[str | int, ...]], Count] = field(default_factory=dict)
    missing: int = 0
    queries: list[SourceQuery] = field(default_factory=list)


def load(
    spec: AnalysisSpec,
    client: CTGovClient,
    canonicalize: Canonicalizer,
    required: Query | None = None,
    max_records: int = MAX_RECORDS,
) -> Dataset:
    """Load what the plan needs. `required` (the request's own filters) applies to every series."""
    series = series_queries(spec, required or Query())
    paths = client.field_paths()
    searched = [name for _, q in series for name in searched_fields(q) if name in paths]
    cited = list(dict.fromkeys(searched))
    fields = sorted({name for d in spec.dimensions for name in FIELDS[d]} | {"BriefTitle", *cited})
    totals = [client.count(query) for _, query in series]
    if not any(totals):
        raise NoTrialsError("No trials match this query.")
    queries = [
        SourceQuery(series=label, url=client.count_url(query), matched=total)
        for (label, query), total in zip(series, totals, strict=True)
    ]
    context = partial(_context, fields=cited, paths=paths)
    if values := _count_plan(spec, client, series, totals, max_records):
        data = _load_counts(spec, client, series, totals, values, fields, context)
        data.queries = queries
        return data

    trials: list[Trial] = []
    matched = fetched = 0
    truncated = False
    for label, query in series:
        result = client.search(query, fields, max_records)
        matched += result.total
        fetched += len(result.studies)
        truncated |= result.truncated
        for study in result.studies:
            nct_id = study["protocolSection"]["identificationModule"]["nctId"]
            facts = {d: EXTRACTORS[d](nct_id, study) for d in spec.dimensions}
            trials.append(Trial(nct_id, label, facts, context(nct_id, study)))

    if not trials:
        raise NoTrialsError("No trials match this query.")

    notes: list[str] = []
    for dimension in LABELED.intersection(spec.dimensions):
        notes += _canonicalize(trials, dimension, canonicalize)
    for trial in trials:
        for dimension in spec.dimensions:
            trial.facts[dimension] = [
                f for f in trial.facts[dimension] if _in_scope(spec, dimension, f.value)
            ]
    if truncated:
        notes.append(f"Only the first {max_records:,} matching trials per series were fetched.")
    return Dataset(trials, matched, fetched, truncated, notes, queries=queries)


def _count_plan(
    spec: AnalysisSpec,
    client: CTGovClient,
    series: list[tuple[str | None, Query]],
    totals: list[int],
    max_records: int,
) -> list[list[str | int]] | None:
    """Values to count per dimension, when counting on the server beats fetching records.

    Fetching costs one request per 1,000 trials and stops at `max_records`; counting costs one
    request per combination of values. Count when it needs fewer requests, or when fetching
    would truncate, as long as the number of counts stays small.
    """
    if spec.chart not in COUNTABLE_CHARTS or not set(spec.dimensions) <= COUNTABLE.keys():
        return None
    pages = sum(math.ceil(t / PAGE_SIZE) for t in totals)
    truncates = max(totals) > max_records
    fixed = math.prod(len(COUNTABLE[d][1]) for d in spec.dimensions if d != Dimension.START_YEAR)
    if not truncates and fixed * len(series) >= pages:
        return None

    values: list[list[str | int]] = []
    for dimension in spec.dimensions:
        if dimension == Dimension.START_YEAR:
            firsts = [client.start_year_bound(q, latest=False) for _, q in series]
            lasts = [client.start_year_bound(q, latest=True) for _, q in series]
            known_first = [y for y in firsts if y]
            known_last = [y for y in lasts if y]
            if not known_first or not known_last:
                return None
            values.append(list(range(min(known_first), max(known_last) + 1)))
        else:
            values.append([v for v in COUNTABLE[dimension][1] if _in_scope(spec, dimension, v)])
    queries = math.prod(len(v) for v in values) * len(series)
    if queries > MAX_COUNT_QUERIES or (not truncates and queries >= pages):
        return None
    return values


def _load_counts(
    spec: AnalysisSpec,
    client: CTGovClient,
    series: list[tuple[str | None, Query]],
    totals: list[int],
    values: list[list[str | int]],
    fields: list[str],
    context: Callable[[str, dict[str, Any]], list[Citation]],
) -> Dataset:
    counts: dict[tuple[str | None, tuple[str | int, ...]], Count] = {}
    fetched = missing = 0
    for label, query in series:
        for combo in product(*values):
            clauses = [_clause(d, v) for d, v in zip(spec.dimensions, combo, strict=True)]
            bucket_query = with_clauses(query, clauses)
            result = client.sample(bucket_query, fields, SAMPLE_SIZE)
            fetched += len(result.studies)
            if not result.total:
                continue
            evidence: dict[tuple[str | None, str], list[Citation]] = {}
            for study in result.studies:
                nct_id = study["protocolSection"]["identificationModule"]["nctId"]
                grouping = [
                    citation
                    for d, value in zip(spec.dimensions, combo, strict=True)
                    for fact in EXTRACTORS[d](nct_id, study)
                    if fact.value == value
                    for citation in fact.evidence
                ]
                evidence[(label, nct_id)] = grouping + context(nct_id, study)
            url = client.count_url(bucket_query)
            counts[(label, combo)] = Count(result.total, evidence, url)
        no_value = " OR ".join(f"AREA[{COUNTABLE[d][0]}]MISSING" for d in spec.dimensions)
        missing += client.count(with_clauses(query, [f"({no_value})"]))
    note = (
        "Counts come straight from ClinicalTrials.gov, one query per value, so they cover every "
        "matching trial; each datum cites a few example trials rather than listing all of them."
    )
    return Dataset(
        [], sum(totals), fetched, False, [note], method="count", counts=counts, missing=missing
    )


def _clause(dimension: Dimension, value: str | int) -> str:
    if dimension == Dimension.START_YEAR:
        return f"AREA[StartDate]RANGE[{value}-01-01,{value}-12-31]"
    return f"AREA[{COUNTABLE[dimension][0]}]{value}"


def series_queries(spec: AnalysisSpec, required: Query) -> list[tuple[str | None, Query]]:
    """The full query of each series: the request's filters AND the plan's shared query AND the
    series' own query. Without a comparison there is one unlabeled series."""
    if not spec.compare:
        return [(None, and_queries(required, spec.query))]
    return [(s.label, and_queries(required, spec.query, s.query)) for s in spec.compare]


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _in_scope(spec: AnalysisSpec, dimension: Dimension, value: object) -> bool:
    """Whether the plan's keep_values / drop_values let this value onto the chart.

    Values compare without case or punctuation, so "Phase 2" matches the API's PHASE2.
    """
    keep = [_norm(v) for v in spec.keep_values.get(dimension, [])]
    drop = [_norm(v) for v in spec.drop_values.get(dimension, [])]
    return (not keep or _norm(value) in keep) and _norm(value) not in drop


def is_estimated(fact: Fact) -> bool:
    return any(
        c.field.endswith("startDateStruct.type") and c.value == "ESTIMATED" for c in fact.evidence
    )


def _canonicalize(
    trials: list[Trial], dimension: Dimension, canonicalize: Canonicalizer
) -> list[str]:
    counts = Counter(str(f.value) for t in trials for f in t.facts[dimension])
    names = [name for name, _ in counts.most_common(MAX_LABELED_NAMES)]
    mapping = canonicalize(dimension, names) if names else {}
    for trial in trials:
        trial.facts[dimension] = _unique(
            Fact(canonical, fact.evidence)
            for fact in trial.facts[dimension]
            for canonical in mapping.get(str(fact.value), [])
        )
    notes = [
        f"{len(names):,} distinct {dimension.value} names were normalized by a language model "
        "(spelling variants merged, combinations split, non-matching entries dropped)."
    ]
    if len(counts) > len(names):
        notes.append(f"{len(counts) - len(names):,} rare {dimension.value} names were not used.")
    return notes


# ---------------------------------------------------------------- extraction

PS = "protocolSection"
STATUS = f"{PS}.statusModule"
SPONSOR = f"{PS}.sponsorCollaboratorsModule.leadSponsor"
INTERVENTIONS = f"{PS}.armsInterventionsModule.interventions"

FIELDS: dict[Dimension, list[str]] = {
    Dimension.START_YEAR: ["StartDate", "StartDateType"],
    Dimension.PHASE: ["Phase"],
    Dimension.STATUS: ["OverallStatus"],
    Dimension.SPONSOR_CLASS: ["LeadSponsorClass"],
    Dimension.LEAD_SPONSOR: ["LeadSponsorName"],
    Dimension.INTERVENTION_TYPE: ["InterventionType"],
    Dimension.DRUG: ["InterventionName", "InterventionType"],
    Dimension.CONDITION: ["Condition"],
    Dimension.COUNTRY: ["LocationCountry"],
    Dimension.ENROLLMENT: ["EnrollmentCount"],
    Dimension.DURATION_MONTHS: ["StartDate", "CompletionDate"],
}


def _at(study: dict[str, Any], path: str) -> Any:
    node: Any = study
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _cite(nct_id: str, field: str, value: Any) -> Citation:
    return Citation(nct_id=nct_id, field=field, value=str(value))


def _unique(facts: Any) -> list[Fact]:
    seen: dict[str | int, Fact] = {}
    for fact in facts:
        seen.setdefault(fact.value, fact)
    return list(seen.values())


def _single(path: str) -> Callable[[str, dict[str, Any]], list[Fact]]:
    def extract(nct_id: str, study: dict[str, Any]) -> list[Fact]:
        value = _at(study, path)
        return [Fact(value, (_cite(nct_id, path, value),))] if value is not None else []

    return extract


def _listed(path: str) -> Callable[[str, dict[str, Any]], list[Fact]]:
    def extract(nct_id: str, study: dict[str, Any]) -> list[Fact]:
        values = _at(study, path) or []
        return _unique(Fact(v, (_cite(nct_id, f"{path}[{i}]", v),)) for i, v in enumerate(values))

    return extract


def _from_list(path: str, key: str, keep: Callable[[dict], bool] = lambda item: True):
    def extract(nct_id: str, study: dict[str, Any]) -> list[Fact]:
        items = _at(study, path) or []
        return _unique(
            Fact(item[key], (_cite(nct_id, f"{path}[{i}].{key}", item[key]),))
            for i, item in enumerate(items)
            if key in item and keep(item)
        )

    return extract


def _start_year(nct_id: str, study: dict[str, Any]) -> list[Fact]:
    raw = _at(study, f"{STATUS}.startDateStruct.date")
    if not raw:
        return []
    evidence = [_cite(nct_id, f"{STATUS}.startDateStruct.date", raw)]
    if date_type := _at(study, f"{STATUS}.startDateStruct.type"):
        evidence.append(_cite(nct_id, f"{STATUS}.startDateStruct.type", date_type))
    return [Fact(int(raw[:4]), tuple(evidence))]


def _duration_months(nct_id: str, study: dict[str, Any]) -> list[Fact]:
    start = _at(study, f"{STATUS}.startDateStruct.date")
    end = _at(study, f"{STATUS}.completionDateStruct.date")
    if not (start and end):
        return []
    months = (int(end[:4]) - int(start[:4])) * 12 + int(end[5:7]) - int(start[5:7])
    if months < 0:
        return []
    evidence = (
        _cite(nct_id, f"{STATUS}.startDateStruct.date", start),
        _cite(nct_id, f"{STATUS}.completionDateStruct.date", end),
    )
    return [Fact(months, evidence)]


TITLE = f"{PS}.identificationModule.briefTitle"

# Search parameters -> the record fields they look in. AREA[Field] terms name theirs directly.
PARAM_FIELDS = {
    "query.cond": ["Condition"],
    "query.intr": ["InterventionName", "InterventionOtherName"],
    "query.spons": ["LeadSponsorName", "CollaboratorName"],
    "query.locn": ["LocationCountry"],
    "filter.overallStatus": ["OverallStatus"],
}
MAX_CITED_VALUES = 12


def searched_fields(query: Query) -> list[str]:
    """Record fields a query searches, in the order they appear."""
    params = query.params()
    names = [name for param in params for name in PARAM_FIELDS.get(param, [])]
    names += re.findall(r"AREA\[(\w+)\]", params.get("filter.advanced", ""))
    return list(dict.fromkeys(names))


def _context(
    nct_id: str, study: dict[str, Any], fields: list[str], paths: dict[str, str]
) -> list[Citation]:
    """The trial's title, and its values of every field the query searched.

    The API decided the match (with synonym expansion for names), so the searched fields are
    cited in full and the reader can see which value matched, e.g. an intervention listed only
    as "MK-3475" for a pembrolizumab search.
    """
    citations = []
    if title := _at(study, TITLE):
        citations.append(Citation(nct_id=nct_id, field=TITLE, value=title, kind="title"))
    for name in fields:
        for path, value in values_at(study, paths[name])[:MAX_CITED_VALUES]:
            citations.append(Citation(nct_id=nct_id, field=path, value=str(value), kind="filter"))
    return citations


def values_at(node: Any, path: str, prefix: str = "") -> list[tuple[str, Any]]:
    """Every value at a metadata path, with list positions filled in (a.b[2].c)."""
    if isinstance(node, list):
        return [
            hit for i, item in enumerate(node) for hit in values_at(item, path, f"{prefix}[{i}]")
        ]
    if not path:
        return [(prefix, node)] if node is not None else []
    key, _, rest = path.partition(".")
    if not isinstance(node, dict) or key not in node:
        return []
    return values_at(node[key], rest, f"{prefix}.{key}" if prefix else key)


EXTRACTORS: dict[Dimension, Callable[[str, dict[str, Any]], list[Fact]]] = {
    Dimension.START_YEAR: _start_year,
    Dimension.PHASE: _listed(f"{PS}.designModule.phases"),
    Dimension.STATUS: _single(f"{STATUS}.overallStatus"),
    Dimension.SPONSOR_CLASS: _single(f"{SPONSOR}.class"),
    Dimension.LEAD_SPONSOR: _single(f"{SPONSOR}.name"),
    Dimension.INTERVENTION_TYPE: _from_list(INTERVENTIONS, "type"),
    Dimension.DRUG: _from_list(INTERVENTIONS, "name", lambda item: item.get("type") in DRUG_TYPES),
    Dimension.CONDITION: _listed(f"{PS}.conditionsModule.conditions"),
    Dimension.COUNTRY: _from_list(f"{PS}.contactsLocationsModule.locations", "country"),
    Dimension.ENROLLMENT: _single(f"{PS}.designModule.enrollmentInfo.count"),
    Dimension.DURATION_MONTHS: _duration_months,
}
