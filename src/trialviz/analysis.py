"""Loads the data an AnalysisSpec needs: fetch trials per series, extract dimension values.

Every extracted value keeps the exact API field and value it came from, so any datum built
from it can cite its source.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trialviz.ctgov import CTGovClient
from trialviz.schemas import AnalysisSpec, Citation, Dimension, Filters

MAX_RECORDS = 10_000
MAX_LABELED_NAMES = 1500
DRUG_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"}

# Free-text dimensions whose spellings vary ("TMZ", "Temodar (temozolomide)"); a model maps
# them to canonical names before grouping.
LABELED = {Dimension.DRUG, Dimension.CONDITION}

Canonicalizer = Callable[[Dimension, list[str]], dict[str, list[str]]]
"""Maps each raw name to canonical names: one for a variant, several for a combination of
drugs, none for something that is not an entity of that kind (placebo, a procedure)."""


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


@dataclass
class Dataset:
    trials: list[Trial]
    matched: int
    fetched: int
    truncated: bool
    notes: list[str]


def load(
    spec: AnalysisSpec,
    client: CTGovClient,
    canonicalize: Canonicalizer,
    max_records: int = MAX_RECORDS,
) -> Dataset:
    fields = sorted({field for d in spec.dimensions for field in FIELDS[d]})
    trials: list[Trial] = []
    matched = fetched = 0
    truncated = False
    for label, filters in series_filters(spec):
        result = client.search(filters, fields, max_records)
        matched += result.total
        fetched += len(result.studies)
        truncated |= result.truncated
        for study in result.studies:
            nct_id = study["protocolSection"]["identificationModule"]["nctId"]
            facts = {d: EXTRACTORS[d](nct_id, study) for d in spec.dimensions}
            trials.append(Trial(nct_id, label, facts))

    if not trials:
        raise NoTrialsError("No trials match these filters.")

    notes: list[str] = []
    for dimension in LABELED.intersection(spec.dimensions):
        notes += _canonicalize(trials, dimension, canonicalize)
    if truncated:
        notes.append(f"Only the first {max_records:,} matching trials per series were fetched.")
    return Dataset(trials, matched, fetched, truncated, notes)


def series_filters(spec: AnalysisSpec) -> list[tuple[str | None, Filters]]:
    """The effective filters of each series; a single unlabeled series without a comparison."""
    if not spec.compare:
        return [(None, spec.filters)]
    return [
        (s.label, spec.filters.model_copy(update=s.filters.model_dump(exclude_defaults=True)))
        for s in spec.compare
    ]


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
