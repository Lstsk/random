from typing import Any

import pytest

from trialviz.ctgov import CTGovClient, SearchResult
from trialviz.schemas import Dimension, Query


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
    identification = {"nctId": nct_id, "briefTitle": f"Study {nct_id}"}
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
            "identificationModule": identification,
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


FIELD_PATHS = {
    "NCTId": "protocolSection.identificationModule.nctId",
    "BriefTitle": "protocolSection.identificationModule.briefTitle",
    "OverallStatus": "protocolSection.statusModule.overallStatus",
    "StartDate": "protocolSection.statusModule.startDateStruct.date",
    "LeadSponsorName": "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
    "LeadSponsorClass": "protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
    "CollaboratorName": "protocolSection.sponsorCollaboratorsModule.collaborators.name",
    "Condition": "protocolSection.conditionsModule.conditions",
    "Phase": "protocolSection.designModule.phases",
    "EnrollmentCount": "protocolSection.designModule.enrollmentInfo.count",
    "InterventionType": "protocolSection.armsInterventionsModule.interventions.type",
    "InterventionName": "protocolSection.armsInterventionsModule.interventions.name",
    "InterventionOtherName": "protocolSection.armsInterventionsModule.interventions.otherNames",
    "LocationCountry": "protocolSection.contactsLocationsModule.locations.country",
}


class FakeClient:
    """Stands in for CTGovClient with canned studies. Given a dict, it returns the studies of
    the first key that appears anywhere in the query, which lets tests compare cohorts."""

    count_url = staticmethod(CTGovClient.count_url)

    def __init__(self, studies: list[dict] | dict[str, list[dict]]) -> None:
        self.studies = studies
        self.queries: list[Query] = []

    def _matching(self, query: Query) -> list[dict]:
        self.queries.append(query)
        if isinstance(self.studies, list):
            return self.studies
        text = str(query.params())
        return next((v for k, v in self.studies.items() if k in text), [])

    def search(self, query: Query, fields, max_records: int) -> SearchResult:
        studies = self._matching(query)
        return SearchResult(studies[:max_records], len(studies), len(studies) > max_records)

    def sample(self, query: Query, fields, size: int) -> SearchResult:
        studies = self._matching(query)
        return SearchResult(studies[:size], len(studies), False)

    def count(self, query: Query) -> int:
        return len(self._matching(query))

    def field_paths(self) -> dict[str, str]:
        return FIELD_PATHS

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
