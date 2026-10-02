"""ClinicalTrials.gov v2 client: query building, paging, throttling, retries and caching."""

import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx2

from trialviz.schemas import API_PARAMS, Filters, Query

BASE_URL = "https://clinicaltrials.gov/api/v2"
PAGE_SIZE = 1000

# The API answers 429 after about ten fast requests and sends no Retry-After header.
# One request per second ran clean in testing; a rejected request clears within ~5 s.
MIN_INTERVAL_S = 1.0
BACKOFF_S = (2, 4, 8)
CACHE_TTL_S = 6 * 3600
MEMORY_CACHE_ENTRIES = 256

_COUNT_ONLY = {"countTotal": "true", "pageSize": "1", "fields": "NCTId"}


class CTGovError(Exception):
    pass


@dataclass
class SearchResult:
    studies: list[dict[str, Any]]
    total: int
    truncated: bool


def request_query(filters: Filters) -> Query:
    """The query that enforces a request's structured filters."""
    clauses: list[str] = []
    if filters.drug_name:
        name = _quote(filters.drug_name)
        # Field-scoped search keeps trials that list the drug as an intervention; the API
        # still expands synonyms (Keytruda, MK-3475 -> pembrolizumab).
        clauses.append(f"(AREA[InterventionName]{name} OR AREA[InterventionOtherName]{name})")
    if filters.country:
        clauses.append(f"AREA[LocationCountry]{_quote(filters.country)}")
    if filters.phases:
        clauses.append(f"AREA[Phase]({' OR '.join(filters.phases)})")
    if filters.start_year or filters.end_year:
        start = f"{filters.start_year}-01-01" if filters.start_year else "MIN"
        end = f"{filters.end_year}-12-31" if filters.end_year else "MAX"
        clauses.append(f"AREA[StartDate]RANGE[{start},{end}]")
    return Query(
        query_cond=filters.condition,
        query_spons=filters.sponsor,
        filter_advanced=" AND ".join(clauses) or None,
        filter_overall_status=filters.statuses,
    )


def and_queries(*queries: Query) -> Query:
    """A query matching only trials that match every one of `queries`."""
    merged: dict[str, Any] = {}
    for name in API_PARAMS:
        parts = [getattr(q, name) for q in queries if getattr(q, name)]
        if parts:
            merged[name] = parts[0] if len(parts) == 1 else " AND ".join(f"({p})" for p in parts)
    status_sets = [set(q.filter_overall_status) for q in queries if q.filter_overall_status]
    if status_sets:
        statuses = set.intersection(*status_sets)
        if not statuses:
            raise ValueError("The status filters exclude each other, so no trial can match.")
        merged["filter_overall_status"] = sorted(statuses)
    return Query(**merged)


def with_clauses(query: Query, clauses: Sequence[str]) -> Query:
    return and_queries(query, Query(filter_advanced=" AND ".join(clauses))) if clauses else query


def _quote(value: str) -> str:
    return '"' + value.replace('"', "").strip() + '"'


class CTGovClient:
    """Thread-safe client. Every request goes through one throttle, retry and cache path.

    `cache_dir` persists responses to disk, which freezes the data for eval runs.
    """

    def __init__(
        self,
        http: httpx2.Client | None = None,
        cache_dir: Path | None = None,
        min_interval_s: float = MIN_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx2.Client(
            base_url=BASE_URL,
            timeout=30,
            headers={"User-Agent": "trialviz/0.1 (clinical-trials visualization service)"},
        )
        self._cache_dir = cache_dir
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}
        self._min_interval_s = min_interval_s
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request = 0.0

    @staticmethod
    def count_url(query: Query) -> str:
        """A browsable URL for the same count request `count` makes."""
        return f"{BASE_URL}/studies?{urlencode(query.params() | _COUNT_ONLY)}"

    def count(self, query: Query) -> int:
        return self._get("/studies", query.params() | _COUNT_ONLY)["totalCount"]

    def sample(self, query: Query, fields: Sequence[str], size: int) -> SearchResult:
        """One request: the exact match count plus the first `size` studies."""
        params = query.params() | {
            "fields": ",".join(["NCTId", *fields]),
            "pageSize": str(size),
            "countTotal": "true",
        }
        body = self._get("/studies", params)
        return SearchResult(body.get("studies", []), body["totalCount"], truncated=False)

    def start_year_bound(self, query: Query, latest: bool) -> int | None:
        """Earliest or latest start year among matching trials (one request)."""
        params = with_clauses(query, ["NOT AREA[StartDate]MISSING"]).params() | {
            "fields": "NCTId,StartDate",
            "pageSize": "1",
            "sort": "StartDate:desc" if latest else "StartDate:asc",
        }
        studies = self._get("/studies", params).get("studies", [])
        date = (
            studies[0]["protocolSection"]["statusModule"]["startDateStruct"]["date"]
            if studies
            else None
        )
        return int(date[:4]) if date else None

    def search(self, query: Query, fields: Sequence[str], max_records: int) -> SearchResult:
        """Fetch matching studies with only `fields`, stopping after `max_records`."""
        params = query.params() | {
            "fields": ",".join(["NCTId", *fields]),
            "pageSize": str(min(PAGE_SIZE, max_records)),
            "countTotal": "true",
        }
        studies: list[dict[str, Any]] = []
        total = 0
        while True:
            body = self._get("/studies", params)
            total = body.get("totalCount", total)
            studies.extend(body.get("studies", []))
            token = body.get("nextPageToken")
            if not token or len(studies) >= max_records:
                break
            params = params | {"pageToken": token}
        return SearchResult(studies[:max_records], total, truncated=total > max_records)

    def data_timestamp(self) -> str | None:
        return self._get("/version", {}).get("dataTimestamp")

    def field_paths(self) -> dict[str, str]:
        """Searchable field name (e.g. InterventionName) -> path in a study record."""
        paths: dict[str, str] = {}

        def walk(nodes: list[dict[str, Any]], prefix: str) -> None:
            for node in nodes:
                path = f"{prefix}.{node['name']}" if prefix else node["name"]
                if node.get("children"):
                    walk(node["children"], path)
                elif node.get("piece"):
                    paths[node["piece"]] = path

        walk(self._get("/studies/metadata", {}), "")
        return paths

    def _get(self, path: str, params: dict[str, str]) -> Any:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if (cached := self._cached(key)) is not None:
            return cached
        body = self._fetch(path, params)
        self._store(key, body)
        return body

    def _fetch(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(len(BACKOFF_S) + 1):
            self._throttle()
            try:
                response = self._http.get(path, params=params)
            except httpx2.TransportError as exc:
                error = f"network error: {exc}"
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code != 429 and response.status_code < 500:
                    raise CTGovError(f"HTTP {response.status_code}: {response.text[:200]}")
                error = f"HTTP {response.status_code}"
            if attempt < len(BACKOFF_S):
                self._sleep(BACKOFF_S[attempt])
        raise CTGovError(f"ClinicalTrials.gov unavailable after retries ({error})")

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request + self._min_interval_s - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()

    def _cached(self, key: str) -> dict[str, Any] | None:
        if key in self._memory:
            stored_at, body = self._memory[key]
            if time.monotonic() - stored_at < CACHE_TTL_S:
                return body
        if self._cache_dir and (path := self._cache_path(key)).exists():
            return json.loads(path.read_text())
        return None

    def _store(self, key: str, body: dict[str, Any]) -> None:
        self._memory[key] = (time.monotonic(), body)
        if len(self._memory) > MEMORY_CACHE_ENTRIES:
            self._memory.pop(next(iter(self._memory)))
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(key).write_text(json.dumps(body))

    def _cache_path(self, key: str) -> Path:
        assert self._cache_dir
        return self._cache_dir / f"{sha256(key.encode()).hexdigest()[:24]}.json"
