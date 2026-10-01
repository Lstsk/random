"""Settings from environment variables, and request settings per model role."""

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.settings import ModelSettings

DEFAULT_PLANNER_MODEL = "openai:gpt-5.4"
DEFAULT_LABELER_MODEL = "openai:gpt-5.4-mini"

# Uses Pydantic AI's provider-neutral settings, so switching provider needs no code change.
# Models that cannot turn thinking off get their lowest level instead.
PLANNER_SETTINGS = ModelSettings(thinking="low", max_tokens=16_000)
LABELER_SETTINGS = ModelSettings(thinking=False, max_tokens=16_000)


@dataclass(frozen=True)
class Settings:
    planner_model: str
    labeler_model: str
    cache_dir: Path | None
    """When set, ClinicalTrials.gov responses are also kept on disk (freezes data for evals)."""

    @classmethod
    def from_env(cls) -> "Settings":
        cache_dir = os.environ.get("CTGOV_CACHE_DIR")
        return cls(
            planner_model=os.environ.get("PLANNER_MODEL", DEFAULT_PLANNER_MODEL),
            labeler_model=os.environ.get("LABELER_MODEL", DEFAULT_LABELER_MODEL),
            cache_dir=Path(cache_dir) if cache_dir else None,
        )
