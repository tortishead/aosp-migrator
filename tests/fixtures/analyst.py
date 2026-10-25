"""A stand-in analyst for tests: answers each backlog item from its brief."""

from __future__ import annotations

import re

from patchmig.agents.analyst import ItemAnalysis

_PATCH = re.compile(r"^### #(\d+) ", re.M)


def analyst(relevance: str = "needed", confidence: float = 0.8, by_key: dict | None = None,
            asked: list | None = None, **extra):  # fmt: skip
    """A responder (task, attempt) -> answer. `by_key` overrides fields per item key."""

    def respond(task, attempt):
        if asked is not None:
            asked.append(task)
        key = task.key.removeprefix("item:")
        ids = [int(i) for i in _PATCH.findall(task.brief)]
        answer = {
            "understanding": f"understanding of {key}",
            "patches": [{"patch_id": i, "problem": f"problem of #{i}", "change": "change",
                         "verification": "verify", "components": ["X"]} for i in ids],
            "relevance": relevance,
            "relevance_reason": f"reason for {key}",
            "solution": {"approach": "cherry_pick", "summary": f"solution for {key}"},
            "sources": ["ticket", "diff"],
            "confidence": confidence,
            **extra,
            **(by_key or {}).get(key, {}),
        }  # fmt: skip
        return ItemAnalysis.model_validate(answer).model_dump()

    return respond


analysis_for = analyst()
