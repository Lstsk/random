"""Normalizes free-text drug and condition names with a cheap model.

The model only maps names it was given to canonical names. Code checks that every name got
exactly one answer, caches answers by name, and never sees a count or a trial.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from trialviz.config import LABELER_SETTINGS
from trialviz.schemas import Dimension

BATCH_SIZE = 150
PARALLEL_BATCHES = 8

INSTRUCTIONS = {
    Dimension.DRUG: (
        "Each line is an intervention name from a clinical-trial registry. Give the canonical "
        "generic name of each active drug or biologic it contains, lowercase (e.g. 'Temodar "
        "(temozolomide)' and 'TMZ' -> ['temozolomide']). Split combinations into each drug. "
        "Return an empty list for anything that is not an active drug: placebo, standard of care, "
        "radiation, procedures, imaging tracers, or unspecified 'chemotherapy'."
    ),
    Dimension.CONDITION: (
        "Each line is a condition name from a clinical-trial registry. Give the canonical "
        "disease name, lowercase, merging spelling variants and abbreviations (e.g. 'GBM' and "
        "'Glioblastoma Multiforme' -> ['glioblastoma']). Return an empty list for entries that "
        "are not a disease or condition."
    ),
}


class Label(BaseModel):
    index: int = Field(description="Line number of the input name.")
    canonical: list[str]


class Labels(BaseModel):
    labels: list[Label]


@dataclass
class Batch:
    names: list[str]


agent = Agent(
    output_type=Labels,
    deps_type=Batch,
    retries={"output": 1},
    defer_model_check=True,
)


@agent.output_validator
def _every_name_answered_once(ctx: RunContext[Batch], output: Labels) -> Labels:
    indices = sorted(label.index for label in output.labels)
    if indices != list(range(len(ctx.deps.names))):
        raise ModelRetry(f"Answer every line 0-{len(ctx.deps.names) - 1} exactly once.")
    return output


class Labeler:
    """Callable used by the analysis to canonicalize names; answers are cached per name."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._cache: dict[tuple[Dimension, str], list[str]] = {}

    def __call__(self, dimension: Dimension, names: list[str]) -> dict[str, list[str]]:
        todo = [n for n in names if (dimension, n) not in self._cache]
        batches = [todo[i : i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
        with ThreadPoolExecutor(PARALLEL_BATCHES) as pool:
            results = pool.map(lambda b: self._label(dimension, b), batches)
            for batch, labels in zip(batches, results, strict=True):
                for label in labels.labels:
                    self._cache[(dimension, batch[label.index])] = [
                        c.strip() for c in label.canonical
                    ]
        return {n: self._cache[(dimension, n)] for n in names}

    def _label(self, dimension: Dimension, names: list[str]) -> Labels:
        prompt = "\n".join(f"{i}: {name}" for i, name in enumerate(names))
        result = agent.run_sync(
            prompt,
            model=self.model,
            instructions=INSTRUCTIONS[dimension],
            deps=Batch(names),
            model_settings=LABELER_SETTINGS,
        )
        return result.output
