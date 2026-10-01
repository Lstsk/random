from conftest import FakeClient, lowercase_labeler, study
from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from trialviz.agent import visualize
from trialviz.schemas import VisualizeRequest

models.ALLOW_MODEL_REQUESTS = False

STUDIES = [
    study("NCT1", start="2019-02", phases=["PHASE2"]),
    study("NCT2", start="2021-07-01", phases=["PHASE3"]),
]
TIME_SERIES = {
    "filters": {"drug_name": "Pembrolizumab"},
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


def ask(model, studies=STUDIES, **request):
    request = VisualizeRequest.model_validate({"query": "How has this changed?"} | request)
    return visualize(request, FakeClient(studies), lowercase_labeler, model)


def test_probe_then_plan_returns_the_chart():
    model, feedback = scripted(
        ("count_trials", {"drug_name": "Pembrolizumab"}), ("run_analysis", TIME_SERIES)
    )
    response = ask(model, drug_name="Pembrolizumab")
    assert response.status == "ok"
    assert feedback == ["2"]
    assert response.visualization.type == "time_series"
    assert response.meta.spec.chart == "time_series"
    assert response.meta.counts.plotted == 2
    assert response.meta.data_timestamp == "2026-10-01T09:00:05"
    assert response.meta.usage["model_requests"] == 2


def test_chart_rule_violation_goes_back_to_the_model():
    bad = TIME_SERIES | {"dimensions": ["phase"]}
    model, feedback = scripted(("run_analysis", bad), ("run_analysis", TIME_SERIES))
    response = ask(model, drug_name="Pembrolizumab")
    assert response.status == "ok"
    assert "time_series needs start_year first" in feedback[0]


def test_dropping_a_request_filter_goes_back_to_the_model():
    dropped = TIME_SERIES | {"filters": {}}
    model, feedback = scripted(("run_analysis", dropped), ("run_analysis", TIME_SERIES))
    response = ask(model, drug_name="pembrolizumab")
    assert response.status == "ok"
    assert "Keep the request filters unchanged" in feedback[0]
    assert "drug_name" in feedback[0]


def test_a_plan_without_filters_goes_back_to_the_model():
    unfiltered = TIME_SERIES | {"filters": {}}
    model, feedback = scripted(("run_analysis", unfiltered), ("run_analysis", TIME_SERIES))
    response = ask(model)
    assert response.status == "ok"
    assert "whole registry" in feedback[0]


def test_no_matching_trials_goes_back_to_the_model():
    model, feedback = scripted(
        ("run_analysis", TIME_SERIES),
        ("cannot_answer", {"reason": "No trials for this drug."}),
    )
    response = ask(model, studies=[], drug_name="Pembrolizumab")
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
    model, _ = scripted(*[("count_trials", {"condition": "x"})] * 10)
    response = ask(model)
    assert response.status == "error"
