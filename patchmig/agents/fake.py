"""A deterministic stand-in for the agent runtime, for tests and demos without Copilot."""

from __future__ import annotations

import json
from collections.abc import Callable

from patchmig.agents.adapter import AgentRuntimeError, AgentTask, RawAnswer

Responder = Callable[[AgentTask, int], str | dict | Exception]


class FakeAdapter:
    """Answers each task from a responder (task, attempt) -> text | dict | exception.

    A dict is returned as a fenced JSON block; an exception is raised as a runtime error.
    Every task seen is kept in ``calls`` for assertions.
    """

    def __init__(self, responder: Responder, model: str | None = None):
        self.responder = responder
        self.model = model
        self.calls: list[tuple[AgentTask, int]] = []

    def run(self, task: AgentTask, attempt: int) -> RawAnswer:
        self.calls.append((task, attempt))
        answer = self.responder(task, attempt)
        if isinstance(answer, Exception):
            raise AgentRuntimeError(str(answer))
        if isinstance(answer, dict):
            answer = f"Done.\n\n```json\n{json.dumps(answer)}\n```\n"
        return RawAnswer(text=answer, duration_s=0.01, model=self.model)
