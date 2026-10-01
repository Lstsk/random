from conftest import FakeClient, lowercase_labeler, study
from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from trialviz.api import Service, app, get_service

models.ALLOW_MODEL_REQUESTS = False

PLAN = {
    "filters": {"condition": "glioblastoma"},
    "dimensions": ["phase"],
    "chart": "bar",
    "title": "Glioblastoma trials by phase",
}


def planner(messages, info) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("run_analysis", PLAN)])


def client() -> TestClient:
    studies = [study("NCT1", phases=["PHASE1"]), study("NCT2", phases=["PHASE1", "PHASE2"])]
    service = Service(FakeClient(studies), lowercase_labeler, FunctionModel(planner))
    app.dependency_overrides[get_service] = lambda: service
    return TestClient(app)


def test_visualize_returns_a_bar_chart():
    response = client().post("/visualize", json={"query": "Glioblastoma trials by phase?"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["visualization"]["encoding"]["x"]["field"] == "phase"
    assert [r["trial_count"] for r in body["visualization"]["data"]] == [2, 1]


def test_invalid_request_is_rejected_before_the_agent_runs():
    response = client().post("/visualize", json={"query": "x", "phases": ["PHASE9"]})
    assert response.status_code == 422


def test_openapi_documents_both_visualization_shapes():
    schema = client().get("/openapi.json").json()
    variants = schema["components"]["schemas"]["VisualizeResponse"]["properties"]["visualization"]
    assert "ChartVisualization" in str(variants) and "NetworkVisualization" in str(variants)
