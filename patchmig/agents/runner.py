"""Runs agent tasks: budget check, call, JSON validation with retry, and ledger logging."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from patchmig.agents.adapter import AgentRuntimeError, AgentTask, LlmAdapter, extract_json
from patchmig.config.schema import BudgetConfig, LlmConfig
from patchmig.ledger import LedgerStore, Run
from patchmig.ledger.models import AgentCall

OUTPUT_KEEP_CHARS = 20_000


class BudgetExceeded(Exception):
    """A call would pass a credit cap: the run's (stop everything) or one cluster's."""

    def __init__(self, message: str, scope: str = "run"):
        super().__init__(message)
        self.scope = scope  # "run" or "cluster"


class AgentFailed(Exception):
    """No valid answer after all attempts; the last error is attached."""


@dataclass
class AgentOutcome:
    data: BaseModel
    attempts: int
    credits: float


class AgentRunner:
    def __init__(
        self,
        adapter: LlmAdapter,
        store: LedgerStore,
        run: Run,
        llm: LlmConfig,
        budget: BudgetConfig,
    ):
        self.adapter = adapter
        self.store = store
        self.run_ = run
        self.llm = llm
        self.budget = budget

    def spent(self, cluster_id: int | None = None) -> float:
        query = select(func.coalesce(func.sum(AgentCall.credits), 0.0)).where(
            AgentCall.run_id == self.run_.id
        )
        if cluster_id is not None:
            query = query.where(AgentCall.cluster_id == cluster_id)
        with self.store.session() as s:
            return float(s.scalar(query))

    def _check_budget(self, task: AgentTask, cost: float) -> None:
        cap = self.budget.run_credits
        if cap is not None and self.spent() + cost > cap:
            raise BudgetExceeded(f"run budget of {cap} credits would be exceeded")
        cap = self.budget.cluster_credits
        cluster = task.cluster_id
        if cap is not None and cluster is not None and self.spent(cluster) + cost > cap:
            raise BudgetExceeded(
                f"cluster {cluster} budget of {cap} credits would be exceeded", scope="cluster"
            )

    def _log(self, task: AgentTask, attempt: int, **fields) -> None:
        with self.store.session() as s, s.begin():
            s.add(
                AgentCall(
                    run_id=self.run_.id,
                    agent=task.agent,
                    task_key=task.key,
                    subject_type=task.subject_type,
                    subject_id=task.subject_id,
                    cluster_id=task.cluster_id,
                    attempt=attempt,
                    prompt_chars=len(task.brief),
                    **fields,
                )
            )

    def run(self, task: AgentTask) -> AgentOutcome:
        model = self.llm.models.get(task.agent)
        cost = self.llm.credits_for(model)
        current = task
        last_error = ""
        credits = 0.0
        for attempt in range(1, self.llm.max_retries + 2):
            self._check_budget(task, cost)
            started = datetime.now(UTC)
            try:
                answer = self.adapter.run(current, attempt)
            except AgentRuntimeError as e:
                self._log(
                    current, attempt, model=model, started_at=started, duration_s=0.0,
                    credits=0.0, ok=False, error=str(e)[:2000], output="",
                )  # fmt: skip
                raise AgentFailed(str(e)) from e
            credits += cost
            try:
                data = task.schema.model_validate(extract_json(answer.text))
                error = None
            except (ValueError, ValidationError) as e:
                data, error = None, str(e)
            self._log(
                current, attempt, model=answer.model or model, started_at=started,
                duration_s=answer.duration_s, credits=cost, ok=data is not None,
                error=error and error[:2000], output=answer.text[-OUTPUT_KEEP_CHARS:],
                output_chars=len(answer.text),
            )  # fmt: skip
            if data is not None:
                return AgentOutcome(data=data, attempts=attempt, credits=credits)
            last_error = error or ""
            current = replace(
                task,
                brief=f"{task.brief}\n\n## Correction\n\nYour previous answer could not be used: "
                f"{last_error[:1500]}\nAnswer again, ending with one valid JSON block.",
            )
        raise AgentFailed(f"no valid answer from {task.agent} for {task.key}: {last_error}")
