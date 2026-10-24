"""Editing the project config from the web front end.

The browser edits the values written in the YAML file (defaults stay implicit) as a form, or
the YAML text itself. A save is validated against the schema first and written only if the
whole config is valid; the file it replaces is kept under `.patchmig/config-history/`.

Secrets stay references. A token typed into the browser is written to its own file (mode
0600) in the secret store, and the config gets a `file:` reference to it. Reads never return
a secret, only its reference and whether it resolves. A store file the saved config stopped
pointing to is deleted.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import secrets as pysecrets
import types
import typing
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import AfterValidator, BaseModel, ValidationError

from patchmig.config.loader import _format_errors
from patchmig.config.schema import ProjectConfig, _check_secret_ref, resolve_secret


class SettingsError(Exception):
    def __init__(self, problems: list[str], conflict: bool = False):
        self.problems = problems
        self.conflict = conflict  # the file changed since the editor read it
        super().__init__("; ".join(problems))


def _is_secret(metadata) -> bool:
    return any(isinstance(m, AfterValidator) and m.func is _check_secret_ref for m in metadata)


def _walk(value, annotation, path: tuple, found: dict[str, str]) -> None:
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is typing.Annotated:
        if _is_secret(annotation.__metadata__):
            if isinstance(value, str):
                found[".".join(map(str, path))] = value
            return
        _walk(value, args[0], path, found)
    elif origin in (typing.Union, types.UnionType):
        for arg in args:
            _walk(value, arg, path, found)
    elif origin is list and isinstance(value, list):
        for i, item in enumerate(value):
            _walk(item, args[0], (*path, i), found)
    elif origin is dict and isinstance(value, dict):
        for key, item in value.items():
            _walk(item, args[1], (*path, key), found)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if isinstance(value, dict):
            for key, item in value.items():
                if field := annotation.model_fields.get(key):
                    if _is_secret(field.metadata):
                        if isinstance(item, str):
                            found[".".join(map(str, (*path, key)))] = item
                    else:
                        _walk(item, field.annotation, (*path, key), found)


def secret_fields(values: dict) -> dict[str, str]:
    """Dotted path -> reference, for every secret field set in raw config values."""
    found: dict[str, str] = {}
    _walk(values, ProjectConfig, (), found)
    return found


def _set_path(values: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = values
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
    if isinstance(node, list):
        node[int(parts[-1])] = value
    else:
        node[parts[-1]] = value


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class SettingsEditor:
    def __init__(self, path: Path, secrets_dir: Path | None = None):
        self.path = Path(path).resolve()
        state = self.path.parent / ".patchmig"
        self.secrets_dir = Path(secrets_dir).resolve() if secrets_dir else state / "secrets"
        self.history_dir = state / "config-history"

    def text(self) -> str:
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return ""

    @staticmethod
    def parse(text: str) -> tuple[dict, list[str]]:
        """Raw values and the problems with them (empty if the config is valid)."""
        try:
            raw = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as e:
            return {}, [f"not valid YAML: {e}"]
        if not isinstance(raw, dict):
            return {}, ["top level must be a mapping"]
        try:
            ProjectConfig.model_validate(raw)
        except ValidationError as e:
            return raw, _format_errors(e)
        return raw, []

    def secret_status(self, values: dict) -> dict[str, dict]:
        out = {}
        for path, ref in secret_fields(values).items():
            try:
                resolve_secret(ref)
                available = True
            except (KeyError, OSError, ValueError):
                available = False
            scheme, _, where = ref.partition(":")
            managed = scheme == "file" and Path(where).parent == self.secrets_dir
            out[path] = {"ref": ref, "available": available, "managed": managed}
        return out

    def view(self) -> dict:
        text = self.text()
        values, problems = self.parse(text)
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "version": version_of(text),
            "yaml": text,
            "values": values,
            "problems": problems,
            "secrets": self.secret_status(values),
            "secrets_dir": str(self.secrets_dir),
            "schema": ProjectConfig.model_json_schema(),
        }

    # ---- saving ---------------------------------------------------------------------------

    def _check_version(self, version: str) -> str:
        current = self.text()
        if version != version_of(current):
            raise SettingsError(
                [f"{self.path.name} was changed by someone else since you opened it; reload"],
                conflict=True,
            )
        return current

    def _secret_file(self, dotted: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", dotted).lstrip(".")
        return self.secrets_dir / f"{slug}-{pysecrets.token_hex(4)}"

    def save(self, values: dict, new_secrets: dict[str, str], version: str, author: str) -> dict:
        """Validate and write form values; new_secrets maps dotted paths to typed tokens."""
        old = self._check_version(version)
        values = copy.deepcopy(values)
        files: dict[Path, str] = {}
        for dotted, secret in new_secrets.items():
            if not secret or not secret.strip():
                raise SettingsError([f"{dotted}: empty secret"])
            target = self._secret_file(dotted)
            files[target] = secret.strip()
            try:
                _set_path(values, dotted, f"file:{target}")
            except (KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
                raise SettingsError([f"{dotted}: no such field"]) from e
        try:
            ProjectConfig.model_validate(values)
        except ValidationError as e:
            raise SettingsError(_format_errors(e)) from e
        known = secret_fields(values)
        if bad := sorted(set(new_secrets) - set(known)):
            raise SettingsError([f"{p}: not a secret field" for p in bad])
        self._write_secrets(files)
        self._write(old, yaml.safe_dump(values, sort_keys=False, allow_unicode=True), author)
        self._prune(old, values)
        return self.view()

    def save_yaml(self, text: str, version: str, author: str) -> dict:
        """Validate and write the YAML text as it is (comments kept)."""
        old = self._check_version(version)
        values, problems = self.parse(text)
        if problems:
            raise SettingsError(problems)
        self._write(old, text if text.endswith("\n") else text + "\n", author)
        self._prune(old, values)
        return self.view()

    def _write_secrets(self, files: dict[Path, str]) -> None:
        if not files:
            return
        self.secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.secrets_dir, 0o700)
        ignore = self.secrets_dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        for target, secret in files.items():
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secret + "\n")

    def _write(self, old: str, new: str, author: str) -> None:
        if old:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            who = re.sub(r"[^A-Za-z0-9_.-]", "_", author or "unknown")[:40]
            (self.history_dir / f"{self.path.stem}-{stamp}-{who}.yaml").write_text(old)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(new)
        if self.path.exists():
            os.chmod(tmp, self.path.stat().st_mode & 0o777)
        os.replace(tmp, self.path)

    def _managed(self, values: dict) -> set[Path]:
        refs = secret_fields(values).values()
        files = {Path(r.partition(":")[2]) for r in refs if r.startswith("file:")}
        return {f for f in files if f.parent == self.secrets_dir}

    def _prune(self, old_text: str, values: dict) -> None:
        """Delete store files the old config pointed to and the new one does not."""
        for f in self._managed(self.parse(old_text)[0]) - self._managed(values):
            f.unlink(missing_ok=True)
