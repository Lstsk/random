from datetime import date

import pytest
from conftest import FakeClient, study

from trialviz import analysis, charts
from trialviz.analysis import NoTrialsError
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
        }
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
    by_drug = {"A": STUDIES[:2], "B": STUDIES[3:]}
    plan = AnalysisSpec(
        dimensions=["phase"],
        chart="grouped_bar",
        title="Phases by drug",
        compare=[
            {"label": "A", "filters": {"drug_name": "A"}},
            {"label": "B", "filters": {"drug_name": "B"}},
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
    fields = {c.field.rsplit(".", 1)[-1] for c in edge.citations}
    assert fields == {"name"}
    assert {n.group for n in network.nodes} == {"lead_sponsor", "drug"}


def test_no_matching_trials_raises(labeler):
    with pytest.raises(NoTrialsError):
        run(labeler, [], dimensions=["phase"], chart="bar")
