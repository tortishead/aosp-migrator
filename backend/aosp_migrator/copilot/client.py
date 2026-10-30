"""The standalone `copilot` CLI, run non-interactively in a scratch checkout.

Tool mode (5.2 spike, CLI 1.0.105, see PLAN-MS5.md): the prompt goes in on stdin (no
argv size limit), `-s` prints only the agent's answer, `--usage-output-file` writes the
request count, premium request cost and tokens per model as JSON. The model sees only
the file tools (`view`, `create`, `edit`, `grep`, `glob`): no shell, web or MCP. File
access is limited to the cwd plus `read_dirs` (`--add-dir`, the temp dir is refused);
writes are allowed in the cwd only: `write(<dir>/**)` is denied for each read dir and
the cwd's `.git`, which the CLI also enforces through symlinks. Anything else it asks for
is refused, as nobody can answer a prompt. Timeout and cancel kill the whole process
group.
"""

import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

EDIT_TOOLS = ("view", "create", "edit", "grep", "glob")
# Seconds between cancel checks.
POLL = 0.2
# Characters of stderr kept for the result.
ERROR_TAIL = 2000

Status = Literal["ok", "failed", "timeout", "canceled"]


@dataclass
class CopilotResult:
    status: Status
    # The agent's final answer (stdout).
    output: str
    exit_code: int | None
    # End of stderr, or why it did not start.
    error: str
    # `--usage-output-file` as written; None when it was not.
    usage: dict[str, Any] | None
    duration: float


def command(binary: str, cwd: Path, model: str | None, read_dirs: Sequence[Path], usage_file: Path, tools: Sequence[str] = EDIT_TOOLS) -> list[str]:
    cwd = cwd.resolve()
    args = [
        binary,
        "-s",
        "--no-auto-update",
        "--no-custom-instructions",
        "--no-ask-user",
        "--disable-builtin-mcps",
        "--disallow-temp-dir",
        "--usage-output-file",
        str(usage_file),
        # Variadic: the next argument must be an option.
        "--available-tools",
        *tools,
        "--allow-tool",
        "write",
        "--deny-tool",
        f"write({cwd}/.git/**)",
    ]
    for directory in read_dirs:
        directory = directory.resolve()
        args += ["--add-dir", str(directory), "--deny-tool", f"write({directory}/**)"]
    if model:
        args += ["--model", model]
    return args


class CopilotClient:
    def __init__(self, binary: str, env: Mapping[str, str] | None = None, kill_grace: float = 5.0) -> None:
        self.binary = binary
        # Added to the server's environment (tests point the fake at its scenarios).
        self._env = dict(env or {})
        self._kill_grace = kill_grace

    def version(self, timeout: float = 30) -> str:
        """`copilot --version`'s first line; raises OSError or subprocess errors when it does not run."""
        result = subprocess.run(
            [self.binary, "--version"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=self._environment(), check=True
        )
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def run(
        self,
        cwd: Path,
        prompt: str,
        model: str | None,
        timeout: float,
        read_dirs: Sequence[Path] = (),
        cancel: Callable[[], bool] = lambda: False,
        tools: Sequence[str] = EDIT_TOOLS,
    ) -> CopilotResult:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="aosp-migrator-copilot-") as tmp:
            prompt_file, out_file, err_file, usage_file = (Path(tmp) / name for name in ("prompt", "out", "err", "usage.json"))
            prompt_file.write_text(prompt)
            args = command(self.binary, cwd, model, read_dirs, usage_file, tools)
            with prompt_file.open() as stdin, out_file.open("w") as stdout, err_file.open("w") as stderr:
                try:
                    process = subprocess.Popen(args, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr, env=self._environment(), start_new_session=True)
                except OSError as exc:
                    return CopilotResult("failed", "", None, f"Copilot CLI did not start: {exc}", None, time.monotonic() - started)
                status = self._wait(process, started + timeout, cancel)
            code = process.returncode
            if status == "ok" and code != 0:
                status = "failed"
            error = err_file.read_text(errors="replace")[-ERROR_TAIL:].strip()
            if status == "timeout":
                error = f"timed out after {timeout:g} s\n{error}".rstrip()
            return CopilotResult(status, out_file.read_text(errors="replace").strip(), code, error, _usage(usage_file), time.monotonic() - started)

    def _wait(self, process: subprocess.Popen[Any], deadline: float, cancel: Callable[[], bool]) -> Status:
        while True:
            try:
                process.wait(POLL)
                return "ok"
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                self._kill(process)
                return "timeout"
            if cancel():
                self._kill(process)
                return "canceled"

    def _kill(self, process: subprocess.Popen[Any]) -> None:
        """SIGTERM to the process group, SIGKILL when it does not end in time."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                process.wait(self._kill_grace)
                return
            except subprocess.TimeoutExpired:
                continue
        process.wait()

    def _environment(self) -> dict[str, str]:
        return {**os.environ, **self._env}


def _usage(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
