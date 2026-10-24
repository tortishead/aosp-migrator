"""One confidence scale for everything Copilot (and the deterministic steps) judge.

Every agent that answers with a `confidence` gets the same scale in its brief, so that 0.8
from the intent writer means what 0.8 from the cluster agent or the reviewer means. The CLI,
the PR descriptions, the report and the front end name the level next to the number.
Review queues still use `thresholds` (quick approve, careful review); the scale is what the
numbers mean, the thresholds are what a project does with them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    name: str
    floor: float  # lowest confidence at this level
    meaning: str


# Highest first. Deterministic results sit on it too: cherry-pick 0.90, net diff 0.85.
SCALE = (
    Level("certain", 0.9, "everything needed was at hand and checked; nothing assumed"),
    Level("confident", 0.75, "one small assumption that could not be checked, named"),
    Level("unsure", 0.5, "plausible, but a person must check the named points"),
    Level("weak", 0.25, "partly guessed; important information is missing"),
    Level("guess", 0.0, "not supported by the evidence; do not rely on it"),
)


def level(confidence: float | None) -> Level | None:
    if confidence is None:
        return None
    shown = round(confidence, 2)  # the level of the number people see
    return next(lv for lv in SCALE if shown >= lv.floor)


def describe(confidence: float | None) -> str:
    """'0.80 (confident)', or '–' when there is none."""
    lv = level(confidence)
    return f"{confidence:.2f} ({lv.name})" if lv else "–"


def scale() -> list[dict]:
    """The levels with their ranges, for the front end."""
    tops = [1.0] + [lv.floor - 0.01 for lv in SCALE[:-1]]
    return [
        {"name": lv.name, "from": lv.floor, "to": round(top, 2), "meaning": lv.meaning}
        for lv, top in zip(SCALE, tops, strict=True)
    ]


def rubric(subject: str) -> str:
    """The scale as a brief section: how to choose `confidence` for `subject`."""
    rows = "\n".join(
        f"- {r['from']:.2f}-{r['to']:.2f} {r['name']}: {r['meaning']}" for r in scale()
    )
    return (
        f"## Confidence scale\n\nGive `confidence` for {subject} on this scale; it decides how "
        f"closely a person reviews your answer. Pick the lowest level whose description fits, "
        f"and name what you could not check.\n\n{rows}\n"
    )
