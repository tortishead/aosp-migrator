"""Candidate graph: which backlog items may depend on or overlap with which, workspace-wide.

Deterministic and cheap; Copilot later confirms or rejects each edge. Edge reasons:

- `files`: two diffs mapped to the same project touch the same file.
- `hunks`: as `files`, and hunks of a shared file overlap: same lines changed, or one
  diff touches lines the other adds (its old side against the other's new side).
- `link`: Jira link between two backlog items.
- `series`: commits `[PATCH n/m]` of one series (same author and m) in different items.
- `symbol`: an identifier one diff adds (class, method, AIDL/HIDL interface, constant,
  resource name, sysprop, permission) appears in another item's diff. Any project.

`from_item` depends on `to_item` when the reason has a direction (link, series, symbol);
otherwise the pair is stored once, lower item id first, with `directed` False in the detail.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.db.models import Attachment, DependencyEdge, DiffScan, Item, ItemLink, JiraIssue
from aosp_migrator.importer.diffparse import FileChange, ParsedDiff, parse_diff

EdgeReason = Literal["files", "hunks", "link", "series", "symbol"]

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SERIES = re.compile(r"^\[PATCH[^\]]*?(\d+)/(\d+)\]")
_EMAIL = re.compile(r"<([^>]+)>")

# Link directions where the item depends on the linked ticket, and the reverse.
_DEPENDS = ("is blocked by", "depends on", "requires", "is caused by")
_DEPENDED = ("blocks", "is depended on by", "is required by", "causes")

# Definitions on added lines. Each pattern's last group is the symbol.
_DEFINITIONS = [
    # class / interface / enum / struct; AIDL parcelable, HIDL union
    re.compile(r"\b(?:class|interface|enum|struct|record|object|parcelable|union)\s+([A-Za-z_]\w*)"),
    # Method or function declaration: `[modifiers] Type [Class::]name(args) [{;]`
    re.compile(r"^\s*(?!(?:return|new|else|throw|case|delete|co_return|yield)\b)(?:[\w<>\[\],.*&@]+\s+)+[*&]?(?:\w+::)*(~?[A-Za-z_]\w*)\s*\([^;{}]*\)?\s*(?:const\s*)?(?:override\s*)?(?:throws\s+[\w., ]+)?[{;]?\s*$"),
    # Constants
    re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)"),
    re.compile(r"\b(?:static\s+)?final\s+[\w<>\[\]]+\s+([A-Z][A-Z0-9_]+)\s*="),
    re.compile(r"\bconst(?:expr)?\s+[\w:<>]+\s+([A-Za-z_]\w*)\s*="),
    # Android resources and manifest permissions
    re.compile(r"<(?:string|dimen|color|bool|integer|drawable|style|array|string-array|integer-array|plurals|attr|declare-styleable|item)\s+name=\"([\w.]+)\""),
    re.compile(r"<permission\b[^>]*android:name=\"([\w.]+)\""),
]
# String literals: system properties and permission names.
_STRING_SYMBOLS = re.compile(
    r"\"((?:ro|persist|sys|vendor|debug|ctl|init|service|bluetooth|dalvik|log|oem|odm)\.[\w.\-]+"
    r"|[a-z][\w]*(?:\.[\w]+)*\.permission\.[A-Z_]+)\""
)
# Words that look like a declaration but say nothing about a dependency.
_STOP = {
    "return", "new", "else", "throw", "case", "if", "while", "for", "switch", "delete", "sizeof", "catch",
    "main", "init", "run", "get", "set", "equals", "hashCode", "toString", "close", "start", "stop",
    "onCreate", "onDestroy", "onStart", "onStop", "onResume", "onPause", "onBind", "onReceive",
    "TAG", "DEBUG", "LOG_TAG", "Builder", "Callback", "Listener", "Stub", "Proxy", "Impl",
}
MIN_SYMBOL = 4
# A hunk's (old-side, new-side) line ranges, end exclusive.
_Range = tuple[int, int]
_Hunk = tuple[_Range, _Range]
_TOKENS = re.compile(r"[A-Za-z_][\w.\-]*")
_WORDS = re.compile(r"[A-Za-z_]\w*")


@dataclass
class ScannedDiff:
    project: str | None  # From the diff scan; None while unmapped
    parsed: ParsedDiff


@dataclass
class GraphItem:
    item_id: int
    diffs: list[ScannedDiff] = field(default_factory=list)
    # (other backlog item id, how this item relates to it, e.g. "is blocked by")
    links: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class Edge:
    from_item: int
    to_item: int
    reason: EdgeReason
    detail: dict[str, Any]

    @property
    def key(self) -> tuple[int, int, str]:
        return (self.from_item, self.to_item, self.reason)


def build_edges(items: Iterable[GraphItem]) -> list[Edge]:
    """All candidate edges between `items`, never an item with itself."""
    items = [i for i in items]
    edges: dict[tuple[int, int, str], Edge] = {}

    def add(edge: Edge) -> None:
        if edge.from_item != edge.to_item:
            edges.setdefault(edge.key, edge)

    known = {i.item_id for i in items}
    for item in items:
        for other, description in item.links:
            if other in known:
                add(_link_edge(item.item_id, other, description))

    facts = [_Facts.of(item) for item in items]
    for i, a in enumerate(facts):
        for b in facts[i + 1 :]:
            if edge := _file_edge(a, b):
                add(edge)
            for edge in _series_edges(a, b):
                add(edge)
        for b in facts:
            if b is not a and (edge := _symbol_edge(a, b)):
                add(edge)
    return list(edges.values())


def _undirected(a: int, b: int, reason: EdgeReason, detail: dict[str, Any]) -> Edge:
    low, high = sorted((a, b))
    return Edge(low, high, reason, {**detail, "directed": False})


def _link_edge(item: int, other: int, description: str) -> Edge:
    lowered = description.casefold()
    if lowered in _DEPENDS:
        return Edge(item, other, "link", {"description": description, "directed": True})
    if lowered in _DEPENDED:
        return Edge(other, item, "link", {"description": _reverse(description), "directed": True})
    return _undirected(item, other, "link", {"description": description})


def _reverse(description: str) -> str:
    return _DEPENDS[_DEPENDED.index(description.casefold())]


@dataclass
class _Facts:
    item_id: int
    # project -> path -> (old-side, new-side) line ranges of its hunks
    files: dict[str, dict[str, list[_Hunk]]]
    # (author email, m) -> patch numbers n
    series: dict[tuple[str, int], set[int]]
    defines: set[str]
    tokens: set[str]

    @classmethod
    def of(cls, item: GraphItem) -> "_Facts":
        files: dict[str, dict[str, list[_Hunk]]] = {}
        series: dict[tuple[str, int], set[int]] = {}
        defines: set[str] = set()
        tokens: set[str] = set()
        for diff in item.diffs:
            if diff.project is not None:
                paths = files.setdefault(diff.project, {})
                for change in diff.parsed.files:
                    for path in {change.path, change.old_path} - {None, ""}:
                        paths.setdefault(path, []).extend(_ranges(change))  # type: ignore[arg-type]
            for commit in diff.parsed.commits:
                if (match := _SERIES.match(commit.subject)) and int(match.group(2)) > 1:
                    email = _EMAIL.search(commit.author or "")
                    author = (email.group(1) if email else commit.author or "").casefold()
                    series.setdefault((author, int(match.group(2))), set()).add(int(match.group(1)))
            added, removed, all_lines = _lines(diff.parsed.files)
            defines |= _defined(added) - _words(removed)
            tokens |= {t for line in all_lines for t in _TOKENS.findall(line)} | _words(all_lines)
        return cls(item.item_id, files, series, defines, tokens)


def _ranges(change: FileChange) -> list[_Hunk]:
    ranges = []
    for hunk in change.hunks:
        if match := _HUNK.match(hunk):
            old_start, old_count, new_start, new_count = (int(g) if g is not None else 1 for g in match.groups())
            # An empty side (added or deleted file) still covers one line, so two diffs adding one file overlap.
            ranges.append(((old_start, old_start + max(old_count, 1)), (new_start, new_start + max(new_count, 1))))
    return ranges


def _lines(changes: list[FileChange]) -> tuple[list[str], list[str], list[str]]:
    added: list[str] = []
    removed: list[str] = []
    every: list[str] = []
    for change in changes:
        for hunk in change.hunks:
            for line in hunk.splitlines()[1:]:
                text = line[1:]
                every.append(text)
                if line.startswith("+"):
                    added.append(text)
                elif line.startswith("-"):
                    removed.append(text)
    return added, removed, every


def _defined(lines: list[str]) -> set[str]:
    symbols: set[str] = set()
    for line in lines:
        for pattern in _DEFINITIONS:
            for match in pattern.finditer(line):
                symbols.add(match.groups()[-1])
        symbols.update(_STRING_SYMBOLS.findall(line))
    return {s for s in symbols if len(s) >= MIN_SYMBOL and s not in _STOP}


def _words(lines: list[str]) -> set[str]:
    return {w for line in lines for w in _WORDS.findall(line)}


def _file_edge(a: _Facts, b: _Facts) -> Edge | None:
    shared: list[str] = []
    overlapping: list[str] = []
    projects: list[str] = []
    for project, a_paths in a.files.items():
        b_paths = b.files.get(project, {})
        common = sorted(a_paths.keys() & b_paths.keys())
        if not common:
            continue
        projects.append(project)
        shared.extend(common)
        overlapping.extend(p for p in common if _overlap(a_paths[p], b_paths[p]))
    if not shared:
        return None
    detail: dict[str, Any] = {"projects": projects, "files": shared}
    if overlapping:
        return _undirected(a.item_id, b.item_id, "hunks", {**detail, "overlapping": overlapping})
    return _undirected(a.item_id, b.item_id, "files", detail)


def _overlap(a: list[_Hunk], b: list[_Hunk]) -> bool:
    def meet(x: _Range, y: _Range) -> bool:
        return x[0] < y[1] and y[0] < x[1]

    return any(meet(a_old, b_old) or meet(a_new, b_old) or meet(a_old, b_new) for a_old, a_new in a for b_old, b_new in b)


def _series_edges(a: _Facts, b: _Facts) -> list[Edge]:
    """Later patch of a series depends on the earlier one."""
    edges = []
    for key in a.series.keys() & b.series.keys():
        author, total = key
        a_max, b_max = max(a.series[key]), max(b.series[key])
        if a.series[key] == b.series[key]:
            continue  # Same patches in both: a duplicate, not an order
        later, earlier = (a, b) if a_max > b_max else (b, a)
        detail = {
            "author": author,
            "from_patch": f"{max(later.series[key])}/{total}",
            "to_patch": f"{min(earlier.series[key])}/{total}",
            "directed": True,
        }
        edges.append(Edge(later.item_id, earlier.item_id, "series", detail))
    return edges


def _symbol_edge(definer: _Facts, user: _Facts) -> Edge | None:
    """`user` depends on `definer` when it mentions symbols `definer` adds."""
    used = sorted(definer.defines & user.tokens)
    if not used:
        return None
    return Edge(user.item_id, definer.item_id, "symbol", {"symbols": used, "directed": True})


def graph_items(session: Session, workspace_id: int) -> list[GraphItem]:
    """Non-dropped backlog items with their current, downloaded diffs and links to other backlog items."""
    rows = session.execute(
        select(Item.id, Item.issue_id).where(Item.workspace_id == workspace_id, Item.dropped.is_(False)).order_by(Item.id)
    )
    items = {item_id: GraphItem(item_id) for item_id, _ in rows}
    by_issue = {issue_id: item_id for item_id, issue_id in session.execute(select(Item.id, Item.issue_id).where(Item.id.in_(items)))}

    diffs = session.execute(
        select(Item.id, Attachment, DiffScan.project)
        .join(JiraIssue, Item.issue_id == JiraIssue.id)
        .join(Attachment, Attachment.issue_id == JiraIssue.id)
        .outerjoin(DiffScan, DiffScan.attachment_id == Attachment.id)
        .where(Item.id.in_(items), Attachment.kind == "diff", Attachment.is_current.is_(True), Attachment.local_path.is_not(None))
        .order_by(Attachment.created, Attachment.id)
    )
    for item_id, attachment, project in diffs:
        if (parsed := read_diff(attachment)) is not None:
            items[item_id].diffs.append(ScannedDiff(project, parsed))

    links = session.execute(select(ItemLink.item_id, ItemLink.issue_id, ItemLink.description).where(ItemLink.item_id.in_(items)))
    for item_id, issue_id, description in links:
        if (other := by_issue.get(issue_id)) is not None:
            items[item_id].links.append((other, description))
    return list(items.values())


def read_diff(attachment: Attachment) -> ParsedDiff | None:
    text = read_diff_text(attachment)
    return parse_diff(text) if text is not None else None


def read_diff_text(attachment: Attachment) -> str | None:
    if attachment.local_path is None:
        return None
    try:
        with open(attachment.local_path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def rebuild_graph(session: Session, workspace_id: int) -> list[DependencyEdge]:
    """Replace the workspace's unconfirmed edges. Edges Copilot confirmed or rejected stay, with fresh detail."""
    new = {edge.key: edge for edge in build_edges(graph_items(session, workspace_id))}
    for row in session.scalars(select(DependencyEdge).where(DependencyEdge.workspace_id == workspace_id)):
        key = (row.from_item_id, row.to_item_id, row.reason)
        if row.copilot is None:
            session.delete(row)
        elif key in new:
            row.detail = new.pop(key).detail
    session.flush()
    for edge in new.values():
        session.add(DependencyEdge(workspace_id=workspace_id, from_item_id=edge.from_item, to_item_id=edge.to_item, reason=edge.reason, detail=edge.detail))
    session.flush()
    return list(session.scalars(select(DependencyEdge).where(DependencyEdge.workspace_id == workspace_id)))
