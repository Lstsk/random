import pytest
from pydantic import TypeAdapter, ValidationError

from trialviz.schemas import AnalysisSpec, VisualizeRequest, VisualizeResponse


def spec(**overrides):
    return AnalysisSpec.model_validate(
        {"dimensions": ["phase"], "chart": "bar", "title": "T"} | overrides
    )


@pytest.mark.parametrize(
    ("dimensions", "chart", "compare"),
    [
        (["phase"], "bar", []),
        (["phase", "sponsor_class"], "grouped_bar", []),
        (["phase"], "grouped_bar", ["A", "B"]),
        (["start_year"], "time_series", []),
        (["start_year", "phase"], "time_series", []),
        (["start_year"], "time_series", ["A", "B"]),
        (["enrollment"], "histogram", []),
        (["enrollment", "duration_months"], "scatter", []),
        (["drug"], "network", []),
        (["lead_sponsor", "drug"], "network", []),
    ],
)
def test_valid_chart_and_dimension_combinations(dimensions, chart, compare):
    series = [{"label": label, "query": {"query_intr": label}} for label in compare]
    spec(dimensions=dimensions, chart=chart, compare=series)


@pytest.mark.parametrize(
    ("dimensions", "chart", "compare"),
    [
        (["start_year"], "bar", []),
        (["enrollment"], "bar", []),
        (["phase"], "grouped_bar", []),
        (["phase"], "time_series", []),
        (["start_year", "phase"], "time_series", ["A", "B"]),
        (["phase"], "histogram", []),
        (["enrollment"], "scatter", []),
        (["phase"], "network", []),
        (["drug", "drug"], "network", []),
        (["drug"], "network", ["A", "B"]),
        (["phase"], "grouped_bar", ["A"]),
    ],
)
def test_invalid_chart_and_dimension_combinations(dimensions, chart, compare):
    series = [{"label": label, "query": {"query_intr": label}} for label in compare]
    with pytest.raises(ValidationError):
        spec(dimensions=dimensions, chart=chart, compare=series)


def test_chart_error_names_the_rule():
    with pytest.raises(ValidationError, match="histogram needs one quantitative dimension"):
        spec(dimensions=["phase"], chart="histogram")


def test_request_rejects_reversed_years_and_unknown_fields():
    with pytest.raises(ValidationError, match="start_year must not be after end_year"):
        VisualizeRequest(query="trials per year", start_year=2020, end_year=2015)
    with pytest.raises(ValidationError):
        VisualizeRequest.model_validate({"query": "trials per year", "drug": "x"})


def test_response_picks_visualization_model_by_type():
    network = {
        "status": "ok",
        "visualization": {
            "type": "network",
            "title": "Sponsors and drugs",
            "data": {"nodes": [], "edges": []},
        },
    }
    response = VisualizeResponse.model_validate(network)
    assert type(response.visualization).__name__ == "NetworkVisualization"


def test_schemas_export_to_json_schema():
    for model in (VisualizeRequest, VisualizeResponse, AnalysisSpec):
        assert TypeAdapter(model).json_schema()
