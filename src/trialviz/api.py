"""HTTP layer: POST /visualize and the OpenAPI docs at /docs."""

from dataclasses import dataclass
from functools import cache
from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic_ai.models import Model

from trialviz import agent
from trialviz.analysis import Canonicalizer
from trialviz.config import Settings
from trialviz.ctgov import CTGovClient
from trialviz.labeler import Labeler
from trialviz.schemas import VisualizeRequest, VisualizeResponse

app = FastAPI(
    title="Clinical trial visualizer",
    description="Answers questions about clinical trials with a visualization spec built from "
    "ClinicalTrials.gov data. Every datum lists the trials behind it and cites their fields.",
    version="0.1.0",
)


@dataclass
class Service:
    client: CTGovClient
    labeler: Canonicalizer
    planner_model: Model | str


@cache
def get_service() -> Service:
    settings = Settings.from_env()
    return Service(
        client=CTGovClient(cache_dir=settings.cache_dir),
        labeler=Labeler(settings.labeler_model),
        planner_model=settings.planner_model,
    )


@app.post("/visualize")
def visualize(
    request: VisualizeRequest, service: Annotated[Service, Depends(get_service)]
) -> VisualizeResponse:
    """Plan an analysis for the question, run it, and return the chart spec.

    Takes 10-40 s: the planner model runs first, then trials are fetched at about one page
    of 1,000 per second to stay inside ClinicalTrials.gov's rate limit.
    """
    return agent.visualize(request, service.client, service.labeler, service.planner_model)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
