from datetime import date

import pytest
from conftest import FIELD_PATHS, FakeClient, study

from trialviz import analysis, charts
from trialviz.analysis import NoTrialsError
from trialviz.ctgov import CTGovClient, SearchResult
from trialviz.schemas import AnalysisSpec

STUDIES = [
    study(
        "NCT00000001",
        start="2020-05",
        completion="2022-05",
        phases=["PHASE1", "PHASE2"],
        interventions=[("DRUG", "Drug A"), ("DRUG", "Drug B + Drug C"), ("DRUG", "Placebo")],
        countries=["United States", "United States", "France"],
        enrollment=100,
        sponsor="Sponsor 1",
    ),
    study(
        "NCT00000002",
        start="2022-01-10",
        start_type="ESTIMATED",
        completion="2023-01-01",
        phases=["PHASE2"],
        interventions=[("DRUG", "drug a"), ("PROCEDURE", "Surgery")],
        countries=["United States"],
        enrollment=40,
        sponsor="Sponsor 1",
    ),
    study("NCT00000003"),
    study(
        "NCT00000004",
        start="2021-03-01",
        phases=["PHASE3"],
        interventions=[("BIOLOGICAL", "Drug B")],
        enrollment=3000,
        sponsor="Sponsor 2",
    ),
]


def run(labeler, studies=STUDIES, **spec):
    plan = AnalysisSpec.model_validate({"title": "Test chart"} | spec)
    data = analysis.load(plan, FakeClient(studies), labeler)
    built = charts.build(plan, data)
    assert_accounted(data, built)
    return built


def assert_accounted(data, built):
    """Every fetched trial is plotted or excluded, and every datum cites only its own trials."""
    assert built.plotted + sum(e.count for e in built.excluded) == len(data.trials)
    viz = built.visualization
    records = viz.data if viz.type != "network" else [e.model_dump() for e in viz.data.edges]
    for record in records:
        if viz.type != "scatter":
            assert record["trial_count"] == len(record["nct_ids"])
        cited = {c["nct_id"] for c in record["citations"]}
        assert cited <= set(record["nct_ids"])


def test_bar_counts_each_listed_phase_in_phase_order(labeler):
    built = run(labeler, dimensions=["phase"], chart="bar")
    rows = {r["phase"]: r for r in built.visualization.data}
    assert list(rows) == ["Phase 1", "Phase 2", "Phase 3"]
    assert rows["Phase 2"]["nct_ids"] == ["NCT00000002", "NCT00000001"]
    assert rows["Phase 1"]["citations"] == [
        {
            "nct_id": "NCT00000001",
            "field": "protocolSection.designModule.phases[0]",
            "value": "PHASE1",
            "kind": "grouping",
        },
        {
            "nct_id": "NCT00000001",
            "field": "protocolSection.identificationModule.briefTitle",
            "value": "Study NCT00000001",
            "kind": "title",
        },
    ]
    assert built.visualization.encoding.x.sort[:2] == ["Early Phase 1", "Phase 1"]
    assert [e.reason for e in built.excluded] == ["no value for a plotted field"]
    assert any("several phase values" in n for n in built.notes)


def test_bar_keeps_top_n_and_counts_a_trial_once_per_country(labeler):
    built = run(labeler, dimensions=["country"], chart="bar", top_n=3)
    rows = {r["country"]: r["trial_count"] for r in built.visualization.data}
    assert rows == {"United States": 2, "France": 1}


def test_time_series_fills_missing_years_and_flags_estimates(labeler):
    built = run(labeler, dimensions=["start_year"], chart="time_series")
    rows = built.visualization.data
    assert [(r["start_year"], r["trial_count"]) for r in rows] == [(2020, 1), (2021, 1), (2022, 1)]
    assert rows[0]["citations"][0]["value"] == "2020-05"
    assert "1 plotted trials have estimated (planned) start dates." in built.notes


def test_time_series_marks_the_current_and_future_years():
    this_year = date.today().year
    studies = [
        study("NCT1", start=f"{this_year}-01-01"),
        study("NCT2", start=f"{this_year + 1}-01"),
    ]
    built = run(None, studies, dimensions=["start_year"], chart="time_series")
    current, future = built.visualization.data
    assert current["partial_period"] and future["projected"]


def test_grouped_bar_compares_series(labeler):
    by_drug = {"drugA": STUDIES[:2], "drugB": STUDIES[3:]}
    plan = AnalysisSpec(
        dimensions=["phase"],
        chart="grouped_bar",
        title="Phases by drug",
        compare=[
            {"label": "A", "query": {"query_intr": "drugA"}},
            {"label": "B", "query": {"query_intr": "drugB"}},
        ],
    )
    data = analysis.load(plan, FakeClient(by_drug), labeler)
    built = charts.build(plan, data)
    assert_accounted(data, built)
    rows = {(r["phase"], r["series"]): r["trial_count"] for r in built.visualization.data}
    assert rows == {("Phase 1", "A"): 1, ("Phase 2", "A"): 2, ("Phase 3", "B"): 1}
    assert built.visualization.encoding.color.field == "series"


def test_histogram_bins_cover_every_value(labeler):
    built = run(labeler, dimensions=["enrollment"], chart="histogram")
    rows = built.visualization.data
    assert sum(r["trial_count"] for r in rows) == 3
    assert all(r["bin_start"] < r["bin_end"] for r in rows)


def test_scatter_plots_one_point_per_trial(labeler):
    built = run(labeler, dimensions=["duration_months", "enrollment"], chart="scatter")
    points = {
        r["nct_id"]: (r["duration_months"], r["enrollment"]) for r in built.visualization.data
    }
    assert points == {"NCT00000001": (24, 100), "NCT00000002": (12, 40)}


def test_drug_network_uses_canonical_names_and_drops_non_drugs(labeler):
    built = run(labeler, dimensions=["drug"], chart="network")
    network = built.visualization.data
    assert {n.label for n in network.nodes} == {"drug a", "drug b", "drug c"}
    assert {(e.source, e.target) for e in network.edges} == {
        ("drug:drug a", "drug:drug b"),
        ("drug:drug a", "drug:drug c"),
        ("drug:drug b", "drug:drug c"),
    }
    drug_b = next(n for n in network.nodes if n.label == "drug b")
    assert drug_b.trial_count == 1


def test_sponsor_drug_network_is_bipartite(labeler):
    built = run(labeler, dimensions=["lead_sponsor", "drug"], chart="network")
    network = built.visualization.data
    edge = next(
        e
        for e in network.edges
        if e.source == "lead_sponsor:Sponsor 1" and e.target == "drug:drug a"
    )
    assert edge.nct_ids == ["NCT00000002", "NCT00000001"]
    grouping = {c.field.rsplit(".", 1)[-1] for c in edge.citations if c.kind == "grouping"}
    assert grouping == {"name"}
    assert {n.group for n in network.nodes} == {"lead_sponsor", "drug"}


def test_no_matching_trials_raises(labeler):
    with pytest.raises(NoTrialsError):
        run(labeler, [], dimensions=["phase"], chart="bar")


class CountingClient:
    """Answers per-value count queries like the API does, from a table of totals."""

    count_url = staticmethod(CTGovClient.count_url)

    def __init__(self, total: int, per_clause: dict[str, int]) -> None:
        self.total, self.per_clause = total, per_clause
        self.requests = 0

    def count(self, query):
        self.requests += 1
        clause = query.filter_advanced
        return self.per_clause.get(clause, 0) if clause else self.total

    def sample(self, query, fields, size):
        self.requests += 1
        total = self.per_clause.get(query.filter_advanced, 0)
        phase = query.filter_advanced.removeprefix("AREA[Phase]")
        studies = [study(f"NCT9{i}", phases=[phase]) for i in range(min(size, total))]
        return SearchResult(studies, total, truncated=False)

    def field_paths(self):
        return FIELD_PATHS

    def start_year_bound(self, query, latest):
        raise AssertionError("not needed for phases")


def test_large_result_sets_are_counted_on_the_server(labeler):
    client = CountingClient(
        total=123_726,
        per_clause={
            "AREA[Phase]PHASE1": 25_148,
            "AREA[Phase]PHASE2": 39_904,
            "AREA[Phase]PHASE3": 13_000,
            "(AREA[Phase]MISSING)": 26_250,
        },
    )
    plan = AnalysisSpec(
        query={"query_cond": "cancer"}, dimensions=["phase"], chart="bar", title="T"
    )
    data = analysis.load(plan, client, labeler)
    built = charts.build(plan, data)

    assert data.method == "count" and data.matched == 123_726
    assert client.requests == 1 + 6 + 1  # total, one per phase value, missing
    rows = {r["phase"]: r for r in built.visualization.data}
    assert rows["Phase 2"]["trial_count"] == 39_904
    assert rows["Phase 2"]["nct_ids"] == ["NCT92", "NCT91", "NCT90"]
    assert rows["Phase 2"]["citations"][0]["field"] == "protocolSection.designModule.phases[0]"
    assert "AREA%5BPhase%5DPHASE2" in rows["Phase 2"]["source_query"]
    assert built.plotted == 123_726 - 26_250
    assert [(e.reason, e.count) for e in built.excluded] == [
        ("no value for a plotted field", 26_250)
    ]


def test_small_result_sets_are_still_fetched(labeler):
    plan = AnalysisSpec(query={"query_cond": "x"}, dimensions=["phase"], chart="bar", title="T")
    data = analysis.load(plan, FakeClient(STUDIES), labeler)
    assert data.method == "fetch"


def resolve(record: dict, path: str):
    """Follow a citation path such as a.b.list[2].name through a study record."""
    node = record
    for part in path.split("."):
        name, _, index = part.partition("[")
        node = node[name]
        if index:
            node = node[int(index.rstrip("]"))]
    return node


@pytest.mark.parametrize(
    "spec",
    [
        {"dimensions": ["phase"], "chart": "bar"},
        {"dimensions": ["start_year"], "chart": "time_series"},
        {"dimensions": ["enrollment"], "chart": "histogram"},
        {"dimensions": ["duration_months", "enrollment"], "chart": "scatter"},
        {"dimensions": ["lead_sponsor", "drug"], "chart": "network"},
    ],
)
def test_every_citation_reads_back_from_the_source_record(labeler, spec):
    records = {s["protocolSection"]["identificationModule"]["nctId"]: s for s in STUDIES}
    query = {
        "query_intr": "Drug A",
        "filter_advanced": 'AREA[LocationCountry]"United States" AND '
        "AREA[StartDate]RANGE[2019-01-01,MAX]",
    }
    plan = AnalysisSpec.model_validate({"title": "T", "query": query} | spec)
    viz = charts.build(plan, analysis.load(plan, FakeClient(STUDIES), labeler)).visualization
    if viz.type == "network":
        data = [n.model_dump() for n in viz.data.nodes] + [e.model_dump() for e in viz.data.edges]
    else:
        data = viz.data
    citations = [c for d in data for c in d["citations"]]
    assert citations
    for c in citations:
        assert str(resolve(records[c["nct_id"]], c["field"])) == c["value"], c


def test_citations_show_why_a_trial_matches_the_filters(labeler):
    record = study(
        "NCT5",
        start="2016-02",
        interventions=[("DRUG", "MK-3475")],
        countries=["Spain", "United States"],
    )
    record["protocolSection"]["armsInterventionsModule"]["interventions"][0]["otherNames"] = [
        "Keytruda"
    ]
    query = {
        "filter_advanced": '(AREA[InterventionName]"pembrolizumab" OR '
        'AREA[InterventionOtherName]"pembrolizumab") AND AREA[LocationCountry]"United States"'
    }
    built = run(labeler, [record], query=query, dimensions=["start_year"], chart="time_series")
    cited = {(c["kind"], c["value"]) for c in built.visualization.data[0]["citations"]}
    assert ("grouping", "2016-02") in cited
    assert ("filter", "MK-3475") in cited and ("filter", "Keytruda") in cited
    assert ("filter", "United States") in cited and ("filter", "Spain") in cited
    assert ("title", "Study NCT5") in cited
