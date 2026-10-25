"""Decide which attachments to download, download them, and parse the diffs.

- Diffs: `.diff` / `.patch`, or small text attachments whose content sniffs as a diff.
- Images: only those embedded in the description or comments.
- Everything else (archives, logs, non-diff text) is not kept; the UI lists it with a Jira link.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from aosp_migrator.importer.diffparse import looks_like_diff, parse_diff
from aosp_migrator.jira.base import AttachmentMeta, JiraClient

AttachmentKind = Literal["diff", "image", "other"]

DIFF_EXTENSIONS = frozenset({".diff", ".patch"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"})
# Text attachments with these extensions (or no extension) are downloaded to sniff for a diff.
SNIFF_EXTENSIONS = frozenset({".txt", ".log", ""})
# Larger text attachments are not sniffed: they are logs, not patches.
SNIFF_MAX_BYTES = 10 * 1024 * 1024
SNIFF_HEAD_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProcessedAttachment:
    meta: AttachmentMeta
    kind: AttachmentKind
    local_path: Path | None = None  # None when not downloaded
    sha256: str | None = None
    parsed: dict[str, Any] | None = None  # ParsedDiff JSON for diffs


def classify(meta: AttachmentMeta) -> Literal["diff", "image", "sniff", "other"]:
    """Kind from metadata alone; `sniff` means the content decides between diff and other."""
    suffix = PurePosixPath(meta.filename.lower()).suffix
    mime = meta.mime.lower()
    if suffix in DIFF_EXTENSIONS or mime in ("text/x-diff", "text/x-patch"):
        return "diff"
    if mime.startswith("image/") or suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in SNIFF_EXTENSIONS and meta.size <= SNIFF_MAX_BYTES and (mime.startswith("text/") or mime == "application/octet-stream"):
        return "sniff"
    return "other"


def local_path(attachments_dir: Path, meta: AttachmentMeta) -> Path:
    """`<attachments_dir>/<jira id>/<filename>`. Ids keep versions with the same filename apart."""
    name = PurePosixPath(meta.filename.replace("\\", "/")).name
    if name in ("", ".", ".."):
        name = "attachment"
    return attachments_dir / meta.jira_id / name


def process_attachments(
    client: JiraClient,
    attachments: list[AttachmentMeta],
    embedded_image_ids: set[str],
    attachments_dir: Path,
) -> list[ProcessedAttachment]:
    """Download diffs and embedded images into `attachments_dir`; parse the diffs.

    Files already on disk are reused: a Jira attachment id never changes content.
    """
    return [_process(client, meta, embedded_image_ids, attachments_dir) for meta in attachments]


def _process(client: JiraClient, meta: AttachmentMeta, embedded_image_ids: set[str], attachments_dir: Path) -> ProcessedAttachment:
    kind = classify(meta)
    if kind == "other" or (kind == "image" and meta.jira_id not in embedded_image_ids):
        return ProcessedAttachment(meta, "image" if kind == "image" else "other")
    path = local_path(attachments_dir, meta)
    sha256 = _sha256(path) if path.exists() else client.download_attachment(meta.url, path)
    if kind == "image":
        return ProcessedAttachment(meta, "image", path, sha256)
    if kind == "sniff":
        with path.open("rb") as f:
            head = f.read(SNIFF_HEAD_BYTES).decode("utf-8", errors="replace")
        if not looks_like_diff(head):
            path.unlink()
            _remove_empty_dir(path.parent)
            return ProcessedAttachment(meta, "other")
    parsed = parse_diff(path.read_bytes().decode("utf-8", errors="replace"))
    return ProcessedAttachment(meta, "diff", path, sha256, parsed.to_json())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
