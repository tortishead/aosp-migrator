"""The single interface through which the tool talks to an LLM agent runtime.

A task is a brief (text), a working directory, a tool policy and a Pydantic schema for the
answer. The runtime returns raw text; the runner extracts and validates the JSON answer.
Copilot CLI is the only production runtime (see copilot.py); FakeAdapter serves tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


@dataclass
class ToolPolicy:
    """What the agent may do. Empty allow list = no tools beyond reading the brief."""

    allow: list[str] = field(default_factory=list)  # e.g. ["patchmig", "shell(git:*)"]
    deny: list[str] = field(default_factory=lambda: ["shell", "write"])
    mcp_servers: dict[str, dict] = field(default_factory=dict)  # name -> server config


@dataclass
class AgentTask:
    agent: str  # one of config.schema.AGENT_NAMES, or "doctor"
    key: str  # stable id of what the task is about, e.g. "patch:12"
    brief: str
    schema: type[BaseModel]
    workdir: Path
    tools: ToolPolicy = field(default_factory=ToolPolicy)
    subject_type: str | None = None  # "patch" / "cluster", for the ledger
    subject_id: int | None = None
    cluster_id: int | None = None  # for per-cluster budgets


@dataclass
class RawAnswer:
    text: str
    duration_s: float
    model: str | None
    exit_code: int = 0
    stderr: str = ""


class AgentRuntimeError(Exception):
    """The runtime could not produce an answer (not installed, timed out, crashed)."""


class LlmAdapter(Protocol):
    def run(self, task: AgentTask, attempt: int) -> RawAnswer: ...


_FENCED = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S)


def extract_json(text: str) -> dict:
    """The last JSON object in an answer: a fenced ```json block, else the last {...} span."""
    for block in reversed(_FENCED.findall(text)):
        try:
            value = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(value, dict):
            found = value  # keep the last top-level object, not objects nested inside it
        i = text.find("{", end)
    if found is None:
        raise ValueError("no JSON object found in the answer")
    return found


ANSWER_INSTRUCTIONS = """
## Answer format

End your answer with exactly one fenced JSON block (```json ... ```) matching this JSON schema.
Do not add fields. Do not wrap it in other text after the block.

```json
{schema}
```
"""


def answer_instructions(schema: type[BaseModel]) -> str:
    return ANSWER_INSTRUCTIONS.format(schema=json.dumps(schema.model_json_schema(), indent=2))
