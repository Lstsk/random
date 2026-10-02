from conftest import FakeClient, lowercase_labeler, study
from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from trialviz.agent import visualize
from trialviz.schemas import VisualizeRequest

models.ALLOW_MODEL_REQUESTS = False

STUDIES = [
    study("NCT1", start="2019-02", phases=["PHASE2"], interventions=[("DRUG", "Nivolumab")]),
    study("NCT2", start="2021-07-01", phases=["PHASE3"], interventions=[("DRUG", "Carboplatin")]),
]
TIME_SERIES = {
    "query": {"query_intr": "pembrolizumab"},
    "dimensions": ["start_year"],
    "chart": "time_series",
    "title": "Pembrolizumab trials by start year",
}


def scripted(*calls: tuple[str, dict]):
    """A planner that makes the given tool calls in order and records what it was told back."""
    feedback: list[str] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        for part in getattr(messages[-1], "parts", []):
            if isinstance(part, RetryPromptPart):
                feedback.append(part.model_response())
            elif isinstance(part, ToolReturnPart):
                feedback.append(str(part.content))
        step = sum(isinstance(m, ModelResponse) for m in messages)
        name, args = calls[step]
        return ModelResponse(parts=[ToolCallPart(name, args)])

    return FunctionModel(respond), feedback


def ask(model, studies=STUDIES, client=None, **request):
    request = VisualizeRequest.model_validate({"query": "How has this changed?"} | request)
    return visualize(request, client or FakeClient(studies), lowercase_labeler, model)


def test_explore_preview_then_finish():
    model, feedback = scripted(
        ("search_studies", {"query": {"query_intr": "pembrolizumab"}, "fields": ["Phase"]}),
        ("count_by", {"query": {"query_intr": "pembrolizumab"}, "field": "InterventionName"}),
        ("preview", TIME_SERIES),
        ("run_analysis", TIME_SERIES),
    )
    response = ask(model)
    assert response.status == "ok"
    search, counted, previewed = feedback
    assert "'total': 2" in search and "'Phase': 'PHASE2'" in search
    assert "{'value': 'Nivolumab', 'trials': 1}" in counted
    assert "'rows': ['2019: 1', '2020: 0', '2021: 1']" in previewed
    assert response.visualization.type == "time_series"
    assert response.meta.counts.plotted == 2
    assert response.meta.data_timestamp == "2026-10-01T09:00:05"
    assert response.meta.usage["model_requests"] == 4


def test_request_filters_are_added_to_every_query():
    client = FakeClient(STUDIES)
    model, _ = scripted(
        ("search_studies", {"query": {"query_cond": "melanoma"}}), ("run_analysis", TIME_SERIES)
    )
    response = ask(model, client=client, drug_name="Pembrolizumab", phases=["PHASE2"])
    assert response.status == "ok"
    assert client.queries
    for query in client.queries:
        assert 'AREA[InterventionName]"Pembrolizumab"' in query.filter_advanced
        assert "AREA[Phase](PHASE2)" in query.filter_advanced


def test_unknown_fields_go_back_with_close_matches():
    model, feedback = scripted(
        ("search_studies", {"query": {"query_cond": "x"}, "fields": ["InterventionNames"]}),
        ("run_analysis", TIME_SERIES),
    )
    ask(model)
    assert "Unknown field 'InterventionNames'" in feedback[0] and "InterventionName" in feedback[0]


def test_chart_rule_violation_goes_back_to_the_model():
    bad = TIME_SERIES | {"dimensions": ["phase"]}
    model, feedback = scripted(("run_analysis", bad), ("run_analysis", TIME_SERIES))
    response = ask(model)
    assert response.status == "ok"
    assert "time_series needs start_year first" in feedback[0]


def test_a_plan_with_an_empty_query_goes_back_to_the_model():
    unfiltered = TIME_SERIES | {"query": {}}
    model, feedback = scripted(("run_analysis", unfiltered), ("run_analysis", TIME_SERIES))
    response = ask(model)
    assert response.status == "ok"
    assert "whole registry" in feedback[0]


def test_drop_values_leave_a_value_off_the_chart():
    plan = {
        "query": {"query_intr": "x"},
        "dimensions": ["drug"],
        "chart": "bar",
        "drop_values": {"drug": ["carboplatin"]},
        "title": "Drugs",
    }
    model, _ = scripted(("run_analysis", plan))
    response = ask(model)
    assert [r["drug"] for r in response.visualization.data] == ["nivolumab"]


def test_no_matching_trials_goes_back_to_the_model():
    model, feedback = scripted(
        ("run_analysis", TIME_SERIES),
        ("cannot_answer", {"reason": "No trials for this drug."}),
    )
    response = ask(model, studies=[])
    assert "No trials match" in feedback[0]
    assert response.status == "cannot_answer"


def test_cannot_answer_returns_the_reason_and_suggestion():
    model, _ = scripted(
        (
            "cannot_answer",
            {
                "reason": "Survival rates are not in the registry.",
                "suggestion": "Phases of Keytruda trials",
            },
        )
    )
    response = ask(model, query="What is the survival rate on Keytruda?")
    assert response.status == "cannot_answer"
    assert response.visualization is None
    assert "Survival rates" in response.message and "Phases of Keytruda" in response.message


def test_a_planner_that_never_finishes_is_an_error():
    model, _ = scripted(*[("search_studies", {"query": {"query_cond": "x"}})] * 20)
    response = ask(model)
    assert response.status == "error"
