from typing import Any

import pytest

from trialviz.ctgov import SearchResult
from trialviz.schemas import Dimension, Filters


def study(
    nct_id: str,
    start: str | None = None,
    start_type: str = "ACTUAL",
    completion: str | None = None,
    phases: list[str] | None = None,
    status: str = "COMPLETED",
    sponsor: str = "Sponsor A",
    sponsor_class: str = "INDUSTRY",
    interventions: list[tuple[str, str]] | None = None,
    conditions: list[str] | None = None,
    countries: list[str] | None = None,
    enrollment: int | None = None,
) -> dict[str, Any]:
    """A study record shaped like the ClinicalTrials.gov v2 API returns it."""
    status_module: dict[str, Any] = {"overallStatus": status}
    if start:
        status_module["startDateStruct"] = {"date": start, "type": start_type}
    if completion:
        status_module["completionDateStruct"] = {"date": completion}
    design: dict[str, Any] = {}
    if phases is not None:
        design["phases"] = phases
    if enrollment is not None:
        design["enrollmentInfo"] = {"count": enrollment}
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "statusModule": status_module,
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": sponsor, "class": sponsor_class}
            },
            "designModule": design,
            "armsInterventionsModule": {
                "interventions": [{"type": t, "name": n} for t, n in interventions or []]
            },
            "conditionsModule": {"conditions": conditions or []},
            "contactsLocationsModule": {"locations": [{"country": c} for c in countries or []]},
        }
    }


class FakeClient:
    """Stands in for CTGovClient; returns canned studies, keyed by drug name when comparing."""

    def __init__(self, studies: list[dict] | dict[str, list[dict]]) -> None:
        self.studies = studies
        self.searches: list[Filters] = []

    def search(self, filters: Filters, fields: list[str], max_records: int) -> SearchResult:
        self.searches.append(filters)
        studies = self.studies
        if isinstance(studies, dict):
            studies = studies.get(filters.drug_name or "", [])
        return SearchResult(studies[:max_records], len(studies), len(studies) > max_records)

    def count(self, filters: Filters) -> int:
        studies = self.studies
        if isinstance(studies, dict):
            return len(studies.get(filters.drug_name or "", []))
        return len(studies)

    def data_timestamp(self) -> str:
        return "2026-10-01T09:00:05"


def lowercase_labeler(dimension: Dimension, names: list[str]) -> dict[str, list[str]]:
    """Canonicalizer stand-in: lowercases names and splits on '+'; drops placebo."""
    return {
        n: [] if "placebo" in n.lower() else [p.strip().lower() for p in n.split("+")]
        for n in names
    }


@pytest.fixture
def labeler():
    return lowercase_labeler
