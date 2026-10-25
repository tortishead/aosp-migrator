"""Find attachment images embedded in Jira's rendered HTML and point them at local copies."""

import html
import re
from collections.abc import Iterable, Mapping
from urllib.parse import urljoin, urlparse

# src/href attribute values. Rendered Jira HTML uses both quote styles.
_ATTR = re.compile(r"""(?P<name>\s(?:src|href))(?P<eq>\s*=\s*)(?P<q>["'])(?P<value>.*?)(?P=q)""", re.IGNORECASE | re.DOTALL)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
# /secure/attachment/<id>/<name> and /secure/thumbnail/<id>/<name>, after an optional context path.
_ATTACHMENT_PATH = re.compile(r"/secure/(?:attachment|thumbnail)/(\d+)/")


def attachment_id(url: str, base_url: str) -> str | None:
    """Jira attachment id that `url` points to, or None if it is not an attachment on this Jira."""
    absolute = urljoin(base_url.rstrip("/") + "/", html.unescape(url))
    parsed, base = urlparse(absolute), urlparse(base_url)
    if (parsed.scheme.lower(), parsed.netloc.lower()) != (base.scheme.lower(), base.netloc.lower()):
        return None
    match = _ATTACHMENT_PATH.search(parsed.path)
    if match is None or not parsed.path.startswith(base.path.rstrip("/") + "/"):
        return None
    return match.group(1)


def embedded_image_ids(bodies: Iterable[str], base_url: str) -> set[str]:
    """Ids of the attachments shown by `<img>` tags in the given HTML bodies."""
    ids: set[str] = set()
    for body in bodies:
        for tag in _IMG_TAG.finditer(body):
            for attr in _ATTR.finditer(tag.group(0)):
                if attr.group("name").strip().lower() == "src":
                    jira_id = attachment_id(attr.group("value"), base_url)
                    if jira_id is not None:
                        ids.add(jira_id)
    return ids


def rewrite_attachment_urls(body: str, base_url: str, local_urls: Mapping[str, str]) -> str:
    """Replace src/href values that point to a Jira attachment with its local URL.

    `local_urls` maps attachment id to local URL; attachments without one keep
    their Jira URL. Thumbnails map to the full image too.
    """

    def replace(attr: re.Match[str]) -> str:
        jira_id = attachment_id(attr.group("value"), base_url)
        local = local_urls.get(jira_id) if jira_id is not None else None
        if local is None:
            return attr.group(0)
        q = attr.group("q")
        return f"{attr.group('name')}{attr.group('eq')}{q}{html.escape(local, quote=True)}{q}"

    return _ATTR.sub(replace, body)
