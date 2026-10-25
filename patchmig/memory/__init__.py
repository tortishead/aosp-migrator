"""Migration memory: what earlier migrations learned, kept in a git repo of YAML and Markdown.

Layout of the repo (config.memory.path):

    patches/<key>.yaml        one file per OEM patch (key: its Change-Id, else its patch-id):
                              subjects, Jira keys, and one history entry per migration with
                              the outcome, strategy, intent, agent summary, PRs and decisions
    tickets/<key>.yaml        one file per Jira ticket: per migration the confirmed analysis
                              (understanding, still needed, solution, related tickets), what
                              engineers added (notes, links, files), the outcome and the PRs
    mappings/<project>.yaml   files that moved or disappeared upstream, per migration
    decisions/<label>.yaml    human decisions and review feedback of one migration
    lessons/proposed/*.md     lessons the memory curator suggested; not used until approved
    lessons/approved/*.md     lessons an engineer approved; given to the cluster agent

Facts (patches, tickets, mappings, decisions) are written deterministically from approved
results, by `migrate memory record`: a ticket only when every cluster holding its patches was
approved, so only confirmed knowledge is kept. Lessons are soft knowledge: the curator agent
proposes them, and only an engineer's approval makes them part of the agent's briefs. Every
change is a commit, so the memory has history and can be reviewed and shared like code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

import yaml
from sqlalchemy import select

from patchmig.config import ProjectConfig
from patchmig.gitutil import git, is_repo
from patchmig.ledger import Cluster, KnowledgeItem, LedgerStore, Patch, Run, State
from patchmig.review import is_approved

_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")  # a lesson's file stem
ORIGINAL_CHANGE_ID = re.compile(r"^Original-Change-Id:\s*(I[0-9a-f]{40})\s*$", re.M)
_IDENTITY = {
    "GIT_AUTHOR_NAME": "patchmig",
    "GIT_AUTHOR_EMAIL": "patchmig@localhost",
    "GIT_COMMITTER_NAME": "patchmig",
    "GIT_COMMITTER_EMAIL": "patchmig@localhost",
}


class MemoryRepoError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def change_ids(patch: Patch) -> list[str]:
    """The Change-Ids a patch carries, including those of patches it was migrated from."""
    ids = [patch.change_id] if patch.change_id else []
    return ids + [c for c in ORIGINAL_CHANGE_ID.findall(patch.message or "") if c not in ids]


def patch_key(patch: Patch) -> str | None:
    ids = change_ids(patch)
    return ids[0] if ids else patch.fingerprint


def _file_key(key: str) -> str:
    """A Jira key as a file name (keys are letters, digits and one dash; nothing else)."""
    return re.sub(r"[^\w.-]+", "_", key)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "lesson"


@dataclass
class Lesson:
    name: str  # file stem
    status: str  # proposed, approved
    title: str
    text: str
    applies_to: list[str] = field(default_factory=list)  # globs over tree paths
    meta: dict = field(default_factory=dict)

    def matches(self, paths: list[str]) -> bool:
        if not self.applies_to:
            return True
        return any(fnmatch(p, g) for p in paths for g in self.applies_to)

    def render(self) -> str:
        front = {"title": self.title, "applies_to": self.applies_to, **self.meta}
        return f"---\n{yaml.safe_dump(front, sort_keys=False)}---\n\n{self.text.strip()}\n"

    @classmethod
    def parse(cls, path: Path, status: str) -> Lesson:
        raw = path.read_text()
        meta, text = {}, raw
        if raw.startswith("---\n"):
            head, _, text = raw[4:].partition("\n---\n")
            meta = yaml.safe_load(head) or {}
            if not isinstance(meta, dict):  # a hand-edited file: keep its text at least
                meta, text = {}, raw
        return cls(
            name=path.stem, status=status, title=meta.pop("title", path.stem),
            text=text.strip(), applies_to=meta.pop("applies_to", []) or [], meta=meta,
        )  # fmt: skip


@dataclass
class RecordReport:
    patches: int = 0
    tickets: int = 0
    mappings: int = 0
    decisions: int = 0
    commit: str | None = None


class Memory:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def open(cls, config: ProjectConfig, create: bool = False) -> Memory:
        if config.memory.path is None:
            raise MemoryRepoError("memory.path is not set in the config")
        mem = cls(config.memory.path)
        if not is_repo(mem.path):
            if not create:
                raise MemoryRepoError(
                    f"{mem.path} is not a git repository (memory record creates it)"
                )
            mem.path.mkdir(parents=True, exist_ok=True)
            git(mem.path, "init", "-q")
        return mem

    # ---- files ------------------------------------------------------------------------

    def _read(self, rel: str) -> dict | None:
        path = self.path / rel
        return yaml.safe_load(path.read_text()) if path.exists() else None

    def _write(self, rel: str, data) -> None:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def commit(self, message: str) -> str | None:
        git(self.path, "add", "-A")
        if not git(self.path, "status", "--porcelain").strip():
            return None
        git(self.path, "commit", "-q", "-m", message, env=_IDENTITY)
        return git(self.path, "rev-parse", "HEAD").strip()

    # ---- reading ----------------------------------------------------------------------

    def history(self, patch: Patch) -> dict | None:
        """The memory of a patch: by any of its Change-Ids, else by its patch-id."""
        for key in [*change_ids(patch), patch.fingerprint]:
            if key and (data := self._read(f"patches/{key}.yaml")):
                return data
        return None

    def ticket(self, key: str) -> dict | None:
        """The memory of a Jira ticket from earlier migrations."""
        return self._read(f"tickets/{_file_key(key)}.yaml")

    def brief_for_ticket(self, key: str, limit: int) -> str:
        """What earlier migrations confirmed about a ticket, at most `limit` chars."""
        lines = []
        for h in ((self.ticket(key) or {}).get("history") or [])[-3:]:
            mig = h["migration"]
            lines.append(f"- {mig}: {h.get('outcome')}; still needed: {h.get('relevance')}: "
                         f"{h.get('relevance_reason')}")  # fmt: skip
            if h.get("solution"):
                lines.append(f"- {mig} solution ({h.get('approach')}): {h['solution']}")
            lines += [f"- {mig} engineer note: {n}" for n in h.get("notes", [])]
            lines += [f"- {mig} related: {r}" for r in h.get("related", [])]
        text = "\n".join(lines)
        return text if len(text) <= limit else text[:limit] + "\n[... more in the memory repo]"

    def mappings(self, project: str) -> dict:
        return (self._read(f"mappings/{project.replace('/', '__')}.yaml") or {}).get("files", {})

    def lessons(self, status: str = "approved") -> list[Lesson]:
        folder = self.path / "lessons" / status
        if not folder.exists():
            return []
        return [Lesson.parse(p, status) for p in sorted(folder.glob("*.md"))]

    def brief_for(self, members: list[Patch], files: list[tuple[str, str]], limit: int) -> str:
        """What the cluster agent should know from earlier migrations, at most `limit` chars.

        `files` are the (project, path) pairs the cluster touches.
        """
        parts, notes = [], []
        for p in members:
            for h in ((self.history(p) or {}).get("history") or [])[-3:]:
                line = f"- #{p.id} {p.subject}: in {h['migration']} {h['outcome']}"
                if h.get("strategy"):
                    line += f" by {h['strategy']}"
                parts.append(line)
                # Summary, decisions and feedback belong to the whole cluster: say them once.
                mig = h["migration"]
                if h.get("summary"):
                    notes.append(f"- {mig} agent summary: {h['summary']}")
                notes += [f"- {mig} decision: {d}" for d in h.get("decisions", [])]
                notes += [f"- {mig} reviewer: {f}" for f in h.get("feedback", [])]
        parts += notes
        for project, path in files:
            if m := self.mappings(project).get(path):
                to = f" -> {m['to']}" if m.get("to") else ""
                parts.append(f"- {project}/{path} was {m['fate']} in {m['migration']}{to}")
        tree_paths = [f"{project}/{path}" for project, path in files]
        for lesson in self.lessons("approved"):
            if lesson.matches(tree_paths):
                parts.append(f"- Lesson ({lesson.title}): {lesson.text}")
        text = "\n".join(dict.fromkeys(parts))
        return text if len(text) <= limit else text[:limit] + "\n[... more in the memory repo]"

    # ---- recording facts --------------------------------------------------------------

    def record(self, config: ProjectConfig, store: LedgerStore, run: Run) -> RecordReport:
        """Write the approved results of a run, its code mappings and decisions; commit."""
        label = config.migration.label
        report = RecordReport()
        with store.session() as s:
            clusters = {c.id: c for c in s.scalars(select(Cluster).where(Cluster.run_id == run.id))}
            patches = s.scalars(
                select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)
            ).all()
            fates = s.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.run_id == run.id, KnowledgeItem.kind == "file_fate"
                )
            ).all()
        for p in patches:
            c = clusters.get(p.cluster_id)
            if c is None or not is_approved(c):
                continue
            key = patch_key(p)
            if key is None:
                continue
            self._record_patch(config, run, p, c, key, label)
            report.patches += 1
        report.tickets = self._record_tickets(config, store, run, clusters, label)
        report.mappings = self._record_mappings(fates, label)
        report.decisions = self._record_decisions(clusters.values(), label, run)
        report.commit = self.commit(f"Record {label} run {run.id}")
        return report

    def _record_patch(self, config, run, p: Patch, c: Cluster, key: str, label: str) -> None:
        rel = f"patches/{key}.yaml"
        data = self._read(rel) or {"key": key, "change_ids": [], "fingerprints": [],
                                    "subjects": [], "jira": [], "history": []}  # fmt: skip
        for name, values in (("change_ids", change_ids(p)), ("fingerprints", [p.fingerprint]),
                             ("subjects", [p.subject]), ("jira", p.jira_keys)):  # fmt: skip
            data[name] = list(dict.fromkeys([*data.get(name, []), *[v for v in values if v]]))
        mig = c.result.get("migration") or {}
        final = (mig.get("agent") or {}).get("final") or {}
        prs = (c.result.get("publish") or {}).get("projects", {})
        entry = {
            "migration": label,
            "from": config.migration.source_version,
            "to": config.migration.target_version,
            "run": run.id,
            "outcome": p.state.value,
            "strategy": ((mig.get("projects") or {}).get(p.project) or {}).get("strategy")
            if p.state == State.VERIFIED else (p.verdict or {}).get("rung"),
            "intent": p.intent,
            "summary": final.get("summary"),
            "prs": [prs[p.project]["url"]] if p.project in prs else [],
            "decisions": [f"{d['choice']}: {d['reason']}" for d in c.result.get("decisions", [])],
            "feedback": [f["text"] for f in c.result.get("feedback", [])],
            "approved_by": (c.result.get("review") or {}).get("by"),
            "recorded_at": _now(),
        }  # fmt: skip
        old = next((h for h in data["history"] if h["migration"] == label), None)
        if old is not None and {**old, "recorded_at": None} == {**entry, "recorded_at": None}:
            entry["recorded_at"] = old["recorded_at"]  # unchanged: no new commit
        history = [h for h in data["history"] if h["migration"] != label]
        data["history"] = [*history, entry]
        self._write(rel, data)

    def _record_tickets(self, config, store: LedgerStore, run: Run, clusters, label) -> int:
        """Each ticket whose patches are all in approved clusters: its confirmed analysis."""
        from patchmig import backlog as bl
        from patchmig import notes as engineer_notes

        notes = engineer_notes.active(store)
        n = 0
        for item in bl.items(store, run):
            members = bl.item_patches(store, item)
            if item.kind != "ticket" or not members:
                continue
            own = [clusters.get(p.cluster_id) for p in members]
            if any(c is None or not is_approved(c) for c in own):
                continue
            a = item.analysis or {}
            sol = a.get("solution") or {}
            prs = sorted(
                {
                    e["url"]
                    for c in own
                    for e in ((c.result.get("publish") or {}).get("projects") or {}).values()
                }
            )
            entry = {
                "migration": label,
                "from": config.migration.source_version,
                "to": config.migration.target_version,
                "run": run.id,
                "title": item.title,
                "outcome": ", ".join(sorted({p.state.value for p in members})),
                "understanding": a.get("understanding"),
                "relevance": a.get("relevance"),
                "relevance_reason": a.get("relevance_reason"),
                "approach": sol.get("approach"),
                "solution": sol.get("summary"),
                "related": [f"{r['ref']} ({r['relation']}): {r['why']}"
                            for r in a.get("related") or [] if r.get("known")],
                "notes": [x.text for x in notes if x.scope == "ticket" and x.target == item.key],
                "added": [x.get("url") or x.get("filename") for x in bl.active_additions(item)],
                "patches": [patch_key(p) for p in members if patch_key(p)],
                "prs": prs,
                "approved_by": sorted({(c.result.get("review") or {}).get("by") for c in own
                                       if (c.result.get("review") or {}).get("by")}),
                "recorded_at": _now(),
            }  # fmt: skip
            rel = f"tickets/{_file_key(item.key)}.yaml"
            data = self._read(rel) or {"key": item.key, "history": []}
            old = next((h for h in data["history"] if h["migration"] == label), None)
            if old is not None and {**old, "recorded_at": None} == {**entry, "recorded_at": None}:
                entry["recorded_at"] = old["recorded_at"]  # unchanged: no new commit
            data["history"] = [*(h for h in data["history"] if h["migration"] != label), entry]
            self._write(rel, data)
            n += 1
        return n

    def _record_mappings(self, fates, label: str) -> int:
        n = 0
        by_project: dict[str, dict] = {}
        for item in fates:
            f = item.data
            if f["status"] not in ("renamed", "moved_project", "deleted"):
                continue
            to = None
            if f.get("new_path"):
                to = f"{f.get('new_project') or f['project']}/{f['new_path']}"
            by_project.setdefault(f["project"], {})[f["path"]] = {
                "fate": f["status"], "to": to, "migration": label,
            }  # fmt: skip
        for project, files in by_project.items():
            rel = f"mappings/{project.replace('/', '__')}.yaml"
            data = self._read(rel) or {"project": project, "files": {}}
            data["files"].update(files)
            self._write(rel, data)
            n += len(files)
        return n

    def _record_decisions(self, clusters, label: str, run: Run) -> int:
        items = []
        for c in sorted(clusters, key=lambda c: c.order_index or 0):
            for d in c.result.get("decisions", []):
                items.append({"cluster": c.name, "kind": "decision", **d})
            for f in c.result.get("feedback", []):
                items.append({"cluster": c.name, "kind": "feedback", **f})
        if not items:
            return 0
        rel = f"decisions/{label}.yaml"
        data = self._read(rel) or {"migration": label, "runs": {}}
        data["runs"][run.id] = items
        self._write(rel, data)
        return len(items)

    # ---- lessons ----------------------------------------------------------------------

    def propose(self, title: str, text: str, applies_to: list[str], meta: dict) -> Lesson:
        name = _slug(title)
        i = 2
        while (self.path / "lessons" / "proposed" / f"{name}.md").exists() or (
            self.path / "lessons" / "approved" / f"{name}.md"
        ).exists():
            name = f"{_slug(title)}-{i}"
            i += 1
        lesson = Lesson(name, "proposed", title, text, applies_to,
                        {**meta, "proposed_at": _now()})  # fmt: skip
        path = self.path / "lessons" / "proposed" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(lesson.render())
        return lesson

    def _proposed(self, name: str) -> Path:
        # A name is a slug (see propose); anything else could point outside the folder.
        src = self.path / "lessons" / "proposed" / f"{name}.md"
        if not _NAME.fullmatch(name) or not src.exists():
            raise MemoryRepoError(f"no proposed lesson '{name}'")
        return src

    def approve(self, name: str, by: str) -> Lesson:
        src = self._proposed(name)
        lesson = Lesson.parse(src, "approved")
        lesson.meta |= {"approved_by": by, "approved_at": _now()}
        dst = self.path / "lessons" / "approved" / f"{name}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(lesson.render())
        src.unlink()
        self.commit(f"Approve lesson {name} ({by})")
        return lesson

    def reject(self, name: str, by: str) -> None:
        self._proposed(name).unlink()
        self.commit(f"Reject lesson {name} ({by})")
