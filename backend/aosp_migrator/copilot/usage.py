"""Copilot usage over the calls rework attempts made (`ReworkAttempt.calls`), for the Copilot dashboard.

A call's `usage` is what the CLI's `--usage-output-file` wrote: `totalPremiumRequestCost` and, per model that
served it, `modelMetrics.<model>.requests.count` and `.usage.inputTokens` / `.outputTokens`. CLIs without that
option report no usage: such calls count as calls, with no premium requests or tokens.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Most recent calls listed.
RECENT_CAP = 50


def _num(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _record(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass
class ModelMetrics:
    model: str
    requests: float
    input_tokens: float
    output_tokens: float


def model_metrics(usage: Any) -> list[ModelMetrics]:
    """Per model that served the call; empty when the CLI wrote no usage."""
    out = []
    for model, metrics in _record(_record(usage).get("modelMetrics")).items():
        m = _record(metrics)
        tokens = _record(m.get("usage"))
        out.append(ModelMetrics(str(model), _num(_record(m.get("requests")).get("count")), _num(tokens.get("inputTokens")), _num(tokens.get("outputTokens"))))
    return out


def premium_requests(usage: Any) -> float:
    return _num(_record(usage).get("totalPremiumRequestCost"))


@dataclass
class CallRef:
    """One Copilot call and the attempt it belongs to."""

    attempt_id: int
    workspace_id: int
    keys: list[str]
    at: datetime
    call: Mapping[str, Any]


@dataclass
class Bucket:
    calls: int = 0
    ok: int = 0
    failed: int = 0
    canceled: int = 0
    # Calls whose CLI reported usage.
    reported: int = 0
    premium_requests: float = 0.0
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    # Seconds.
    duration: float = 0.0

    def add(self, call: Mapping[str, Any]) -> None:
        self.calls += 1
        status = call.get("status")
        if status == "ok":
            self.ok += 1
        elif status == "canceled":
            self.canceled += 1
        else:
            self.failed += 1
        usage = call.get("usage")
        if isinstance(usage, Mapping):
            self.reported += 1
        self.premium_requests += premium_requests(usage)
        for m in model_metrics(usage):
            self.input_tokens += m.input_tokens
            self.output_tokens += m.output_tokens
        self.duration += _num(call.get("duration"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "ok": self.ok,
            "failed": self.failed,
            "canceled": self.canceled,
            "reported": self.reported,
            "premium_requests": self.premium_requests,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "duration": round(self.duration, 1),
        }


@dataclass
class ModelBucket:
    # Calls it served, model API requests (one per tool round), tokens.
    calls: int = 0
    requests: float = 0.0
    input_tokens: float = 0.0
    output_tokens: float = 0.0


@dataclass
class Summary:
    totals: Bucket = field(default_factory=Bucket)
    attempts: set[int] = field(default_factory=set)
    by_rung: dict[str, Bucket] = field(default_factory=dict)
    by_day: dict[str, Bucket] = field(default_factory=dict)
    by_workspace: dict[int, Bucket] = field(default_factory=dict)
    # By the model that served the call, from its usage.
    by_model: dict[str, ModelBucket] = field(default_factory=dict)
    # By the model asked for: None is the CLI's default.
    by_requested_model: dict[str | None, Bucket] = field(default_factory=dict)


def summarize(calls: Iterable[CallRef], workspace_names: Mapping[int, str]) -> dict[str, Any]:
    """Totals and breakdowns by rung, day, workspace and model, plus the most recent calls, newest first."""
    summary = Summary()
    refs = sorted(calls, key=lambda r: (r.at, r.attempt_id), reverse=True)
    for ref in refs:
        call = ref.call
        summary.totals.add(call)
        summary.attempts.add(ref.attempt_id)
        summary.by_rung.setdefault(str(call.get("rung") or "unknown"), Bucket()).add(call)
        summary.by_day.setdefault(ref.at.date().isoformat(), Bucket()).add(call)
        summary.by_workspace.setdefault(ref.workspace_id, Bucket()).add(call)
        requested = call.get("model") or None
        summary.by_requested_model.setdefault(requested, Bucket()).add(call)
        for m in model_metrics(call.get("usage")):
            bucket = summary.by_model.setdefault(m.model, ModelBucket())
            bucket.calls += 1
            bucket.requests += m.requests
            bucket.input_tokens += m.input_tokens
            bucket.output_tokens += m.output_tokens

    return {
        "totals": {**summary.totals.to_dict(), "attempts": len(summary.attempts)},
        "by_rung": [{"rung": rung, **b.to_dict()} for rung, b in sorted(summary.by_rung.items(), key=lambda kv: -kv[1].calls)],
        "by_day": [{"day": day, **b.to_dict()} for day, b in sorted(summary.by_day.items())],
        "by_workspace": [
            {"workspace_id": wid, "name": workspace_names.get(wid, f"#{wid}"), **b.to_dict()}
            for wid, b in sorted(summary.by_workspace.items(), key=lambda kv: -kv[1].calls)
        ],
        "by_model": [
            {"model": model, "calls": b.calls, "requests": int(b.requests), "input_tokens": int(b.input_tokens), "output_tokens": int(b.output_tokens)}
            for model, b in sorted(summary.by_model.items(), key=lambda kv: -(kv[1].input_tokens + kv[1].output_tokens))
        ],
        "by_requested_model": [
            {"model": model, **b.to_dict()} for model, b in sorted(summary.by_requested_model.items(), key=lambda kv: -kv[1].calls)
        ],
        "recent": [_recent(ref, workspace_names) for ref in refs[:RECENT_CAP]],
    }


def _recent(ref: CallRef, workspace_names: Mapping[int, str]) -> dict[str, Any]:
    call = ref.call
    usage = call.get("usage")
    metrics = model_metrics(usage)
    return {
        "attempt_id": ref.attempt_id,
        "workspace_id": ref.workspace_id,
        "workspace": workspace_names.get(ref.workspace_id, f"#{ref.workspace_id}"),
        "keys": ref.keys,
        "at": ref.at,
        "project": call.get("project"),
        "rung": call.get("rung"),
        "round": call.get("round", 0),
        "build_round": call.get("build_round", 0),
        "model": call.get("model"),
        "served_models": [m.model for m in metrics],
        "status": call.get("status"),
        "premium_requests": premium_requests(usage) if isinstance(usage, Mapping) else None,
        "input_tokens": int(sum(m.input_tokens for m in metrics)) if metrics else None,
        "output_tokens": int(sum(m.output_tokens for m in metrics)) if metrics else None,
        "duration": round(_num(call.get("duration")), 1),
    }
