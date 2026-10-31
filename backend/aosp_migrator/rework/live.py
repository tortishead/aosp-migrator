"""The Copilot call a rework attempt is making now, for the job panel and the Copilot page.

While a call runs, `live.json` in the attempt's prompts directory names it; the CLI's stdout and stderr go to
`call-<n>.out` / `call-<n>.err` next to its prompt, so their end can be shown as the call's log.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LIVE = "live.json"
# Lines and bytes of the call's output shown.
TAIL_LINES = 30
TAIL_BYTES = 16_000


def prompts_dir(workspace_dir: Path, attempt_id: int) -> Path:
    return workspace_dir / "rework-prompts" / str(attempt_id)


def start(directory: Path, stem: Path, call: dict[str, Any]) -> None:
    """Name the call starting now; `stem` is where its `.out` / `.err` go."""
    data = {**call, "stem": str(stem), "started_at": datetime.now(UTC).isoformat()}
    path = directory / LIVE
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    temp.replace(path)


def clear(directory: Path) -> None:
    (directory / LIVE).unlink(missing_ok=True)


def read(directory: Path) -> dict[str, Any] | None:
    """The running call: what `start` got, `started_at`, `elapsed` seconds and `log` (end of stderr, then stdout); None when none."""
    try:
        data = json.loads((directory / LIVE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stem = Path(str(data.pop("stem", "")))
    try:
        started = datetime.fromisoformat(str(data.get("started_at")))
        data["elapsed"] = max(0.0, (datetime.now(UTC) - started).total_seconds())
    except ValueError:
        data["elapsed"] = 0.0
    parts = [_tail(stem.with_suffix(suffix)) for suffix in (".err", ".out")]
    data["log"] = "\n".join(p for p in parts if p)
    return data


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as f:
            f.seek(max(0, f.seek(0, 2) - TAIL_BYTES))
            text = f.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-TAIL_LINES:])
