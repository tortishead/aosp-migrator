"""Where builds and tests run: a build server over SSH, or this machine.

Both run a bash script in a login shell and return its combined output. SSH goes through
the system `ssh` client (BatchMode, so it never prompts), which keeps the user's ssh config,
agent and known_hosts in charge, as git is driven through its CLI. The key is a secret
reference to a key *file*; its contents are never read or logged.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from patchmig.config.schema import BuildConfig, SshConfig


@dataclass
class CommandResult:
    exit_code: int
    output: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def tail(self, lines: int) -> str:
        return "\n".join(self.output.splitlines()[-lines:])


class BuildHost(Protocol):
    name: str

    def run(
        self, script: str, input: str | None = None, timeout: float | None = None
    ) -> CommandResult: ...


def _run(cmd: list[str], input: str | None, timeout: float | None, env=None) -> CommandResult:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
            stdin=None if input is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return CommandResult(124, out + f"\n[timed out after {timeout:.0f}s]", timeout, True)
    except FileNotFoundError as e:
        return CommandResult(127, str(e), time.monotonic() - start)
    return CommandResult(proc.returncode, proc.stdout, time.monotonic() - start)


def key_path(ref: str) -> Path:
    """The key file a secret reference names: file:<path>, or env:<VAR> holding a path."""
    scheme, _, rest = ref.partition(":")
    value = os.environ[rest] if scheme == "env" else rest
    return Path(value).expanduser()


class SshHost:
    def __init__(self, ssh: SshConfig, executable: str = "ssh"):
        self.ssh = ssh
        self.executable = executable
        self.name = f"{ssh.user}@{ssh.host}"

    def command(self, script: str) -> list[str]:
        cmd = [
            self.executable,
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=20",
            "-o", "ServerAliveInterval=60",
            "-p", str(self.ssh.port),
        ]  # fmt: skip
        if self.ssh.key:
            cmd += ["-i", str(key_path(self.ssh.key)), "-o", "IdentitiesOnly=yes"]
        return cmd + [self.name, "bash -lc " + shlex.quote(script)]

    def run(self, script, input=None, timeout=None) -> CommandResult:
        return _run(self.command(script), input, timeout)


class LocalHost:
    name = "local"

    def run(self, script, input=None, timeout=None) -> CommandResult:
        return _run(["bash", "-lc", script], input, timeout)


def host_for(build: BuildConfig) -> BuildHost:
    return LocalHost() if build.transport == "local" else SshHost(build.ssh)
