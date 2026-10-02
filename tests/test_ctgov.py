import json

import httpx2
import pytest

from trialviz.ctgov import CTGovClient, CTGovError, and_queries, request_query
from trialviz.schemas import Filters, Query


def test_request_filters_become_a_query():
    query = request_query(
        Filters(
            drug_name="Pembrolizumab",
            condition="melanoma",
            country="United States",
            phases=["PHASE2", "PHASE3"],
            statuses=["RECRUITING"],
            start_year=2015,
        )
    )
    assert query.params() == {
        "query.cond": "melanoma",
        "filter.advanced": '(AREA[InterventionName]"Pembrolizumab" OR '
        'AREA[InterventionOtherName]"Pembrolizumab") AND '
        'AREA[LocationCountry]"United States" AND AREA[Phase](PHASE2 OR PHASE3) AND '
        "AREA[StartDate]RANGE[2015-01-01,MAX]",
        "filter.overallStatus": "RECRUITING",
    }


def test_quotes_in_names_cannot_break_the_query():
    query = request_query(Filters(drug_name='x" OR AREA[Phase]PHASE1 "'))
    assert query.filter_advanced.count('"') == 4


def test_and_queries_requires_every_part():
    merged = and_queries(
        Query(query_cond="melanoma", filter_overall_status=["RECRUITING", "COMPLETED"]),
        Query(query_cond="stage IV", filter_advanced="AREA[Phase]PHASE3"),
        Query(filter_overall_status=["RECRUITING"]),
    )
    assert merged.query_cond == "(melanoma) AND (stage IV)"
    assert merged.filter_advanced == "AREA[Phase]PHASE3"
    assert merged.filter_overall_status == ["RECRUITING"]


def test_and_queries_rejects_statuses_that_exclude_each_other():
    with pytest.raises(ValueError, match="exclude each other"):
        and_queries(
            Query(filter_overall_status=["RECRUITING"]), Query(filter_overall_status=["COMPLETED"])
        )


def client_with(handler, cache_dir=None) -> tuple[CTGovClient, list[float]]:
    sleeps: list[float] = []
    http = httpx2.Client(base_url="https://ct.test", transport=httpx2.MockTransport(handler))
    client = CTGovClient(http=http, cache_dir=cache_dir, min_interval_s=0, sleep=sleeps.append)
    return client, sleeps


def test_search_follows_pages_and_stops_at_the_cap():
    def handler(request: httpx2.Request) -> httpx2.Response:
        token = request.url.params.get("pageToken")
        page = int(token) if token else 0
        studies = [{"id": f"{page}-{i}"} for i in range(2)]
        body = {"totalCount": 7, "studies": studies, "nextPageToken": str(page + 1)}
        return httpx2.Response(200, json=body)

    client, _ = client_with(handler)
    result = client.search(Query(query_cond="x"), ["Phase"], max_records=5)
    assert len(result.studies) == 5
    assert result.total == 7
    assert result.truncated


def test_retries_rate_limits_with_backoff():
    responses = iter(
        [httpx2.Response(429), httpx2.Response(503), httpx2.Response(200, json={"totalCount": 3})]
    )
    client, sleeps = client_with(lambda request: next(responses))
    assert client.count(Query(query_cond="x")) == 3
    assert sleeps == [2, 4]


def test_gives_up_after_the_last_backoff():
    client, sleeps = client_with(lambda request: httpx2.Response(429))
    with pytest.raises(CTGovError, match="unavailable after retries"):
        client.count(Query(query_cond="x"))
    assert sleeps == [2, 4, 8]


def test_client_errors_are_not_retried():
    client, sleeps = client_with(lambda request: httpx2.Response(400, text="bad query"))
    with pytest.raises(CTGovError, match="HTTP 400"):
        client.count(Query(query_cond="x"))
    assert sleeps == []


def test_repeated_requests_are_served_from_cache(tmp_path):
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"totalCount": 3})

    client, _ = client_with(handler, cache_dir=tmp_path)
    client.count(Query(query_cond="x"))
    client.count(Query(query_cond="x"))
    assert len(calls) == 1
    [cached] = tmp_path.iterdir()
    assert json.loads(cached.read_text()) == {"totalCount": 3}
