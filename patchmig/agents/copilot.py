"""GitHub Copilot CLI as the agent runtime, run headless: one process per task.

Flags follow the Copilot CLI programmatic reference (-p, -s, --no-ask-user, --model,
--allow-tool, --deny-tool, --attachment). The MCP flag and any extra arguments come from
config so a CLI version difference can be fixed without a code change; `migrate doctor`
checks them on the real machine.

Sign-in: with `llm.github_token` the CLI gets the token in its environment (never on the
command line), for the host in `llm.github_host`; without it, the CLI's own `copilot login`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from patchmig.agents.adapter import AgentRuntimeError, AgentTask, RawAnswer
from patchmig.config.schema import LlmConfig, resolve_secret


def github_kind(host: str | None) -> str:
    """github.com, GHE.com (data residency) or GitHub Enterprise Server."""
    host = (host or "github.com").lower()
    if host == "github.com":
        return "github.com"
    return "ghe.com" if host.endswith(".ghe.com") else "ghes"


def github_api(host: str | None) -> str:
    kind, host = github_kind(host), host or "github.com"
    if kind == "ghes":
        return f"https://{host}/api/v3"
    return f"https://api.{host}"


def copilot_env(config: LlmConfig) -> dict[str, str]:
    """Environment variables that sign the CLI in. Contains the secret: never log it."""
    env: dict[str, str] = {}
    kind, host = github_kind(config.github_host), config.github_host
    token = resolve_secret(config.github_token) if config.github_token else None
    if kind == "ghes":  # technical preview: docs.github.com/en/copilot/copilot-on-ghes
        env |= {"COPILOT_PROVIDER_GHES_HOST": host, "COPILOT_OFFLINE": "true", "GH_HOST": host}
        if token:
            env |= {"COPILOT_PROVIDER_GHES_TOKEN": token, "GH_ENTERPRISE_TOKEN": token}
        return env
    if kind == "ghe.com":
        env["GH_HOST"] = host
    if token:
        env["COPILOT_GITHUB_TOKEN"] = token
    return env


class CopilotCliAdapter:
    def __init__(self, config: LlmConfig, scratch: Path):
        self.config = config
        self.scratch = scratch  # where long briefs are written as attachments

    def model_for(self, agent: str) -> str | None:
        return self.config.models.get(agent)

    def command(self, task: AgentTask, attempt: int) -> list[str]:
        cfg = self.config
        cmd = [cfg.executable]
        if len(task.brief) <= cfg.inline_prompt_max_chars:
            cmd += ["-p", task.brief]
        else:
            self.scratch.mkdir(parents=True, exist_ok=True)
            safe_key = task.key.replace(":", "-").replace("/", "_")
            brief_file = self.scratch / f"{task.agent}-{safe_key}-{attempt}.md"
            brief_file.write_text(task.brief)
            cmd += [
                "-p",
                "Your full task is in the attached file. Follow it exactly.",
                f"--attachment={brief_file}",
            ]
        cmd += ["-s", "--no-ask-user"]
        if model := self.model_for(task.agent):
            cmd.append(f"--model={model}")
        if task.tools.allow:
            cmd.append(f"--allow-tool={', '.join(task.tools.allow)}")
        if task.tools.deny:
            cmd.append(f"--deny-tool={', '.join(task.tools.deny)}")
        if task.tools.mcp_servers:
            cmd += [cfg.mcp_flag, json.dumps({"mcpServers": task.tools.mcp_servers})]
        return cmd + cfg.extra_args

    def run(self, task: AgentTask, attempt: int) -> RawAnswer:
        cmd = self.command(task, attempt)
        try:
            sign_in = copilot_env(self.config)
        except (KeyError, OSError) as e:
            raise AgentRuntimeError(f"llm.github_token not available: {e}") from e
        env = {**os.environ, "COPILOT_AUTO_UPDATE": "false", **sign_in}
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=task.workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise AgentRuntimeError(
                f"Copilot CLI not found as '{self.config.executable}'; install it or set "
                "llm.executable"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise AgentRuntimeError(
                f"Copilot CLI timed out after {self.config.timeout_seconds}s"
            ) from e
        duration = time.monotonic() - start
        if proc.returncode != 0:
            raise AgentRuntimeError(
                f"Copilot CLI exited with {proc.returncode}: {proc.stderr.strip()[-2000:]}"
            )
        return RawAnswer(
            text=proc.stdout,
            duration_s=duration,
            model=self.model_for(task.agent),
            exit_code=proc.returncode,
            stderr=proc.stderr,
        )
