from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_jira import FakeJiraStore
from fake_jira.fixtures import PNG, fixture_file

from aosp_migrator.importer.attachments import ProcessedAttachment, classify, local_path, process_attachments
from aosp_migrator.importer.html import attachment_id, embedded_image_ids, rewrite_attachment_urls
from aosp_migrator.jira.base import AttachmentMeta, Issue
from aosp_migrator.jira.datacenter import DataCenterClient


@pytest.fixture
def client(jira_url: str, fake_jira: FakeJiraStore) -> Iterator[DataCenterClient]:
    with DataCenterClient(jira_url, fake_jira.pat, sleep=lambda _: None) as client:
        yield client


def meta(filename: str, mime: str = "text/plain", size: int = 100, jira_id: str = "1") -> AttachmentMeta:
    return AttachmentMeta(jira_id, filename, size, mime, datetime(2026, 9, 1, tzinfo=UTC), f"/secure/attachment/{jira_id}/{filename}")


def process(client: DataCenterClient, issue: Issue, tmp_path: Path) -> dict[str, ProcessedAttachment]:
    bodies = [issue.description_html] + [c.body_html for c in client.get_comments(issue.key)]
    images = embedded_image_ids(bodies, client.base_url)
    processed = process_attachments(client, issue.attachments, images, tmp_path / "attachments")
    return {f"{p.meta.filename}@{p.meta.jira_id}": p for p in processed}


@pytest.mark.parametrize(
    ("filename", "mime", "expected"),
    [
        ("0001-fix.patch", "text/x-patch", "diff"),
        ("change.DIFF", "application/octet-stream", "diff"),
        ("fix", "text/x-diff", "diff"),
        ("screenshot.png", "image/png", "image"),
        ("photo.JPG", "application/octet-stream", "image"),
        ("fix.txt", "text/plain", "sniff"),
        ("dump", "application/octet-stream", "sniff"),
        ("logs.zip", "application/zip", "other"),
        ("logs.tar.gz", "application/gzip", "other"),
        ("report.pdf", "application/pdf", "other"),
    ],
)
def test_classify(filename: str, mime: str, expected: str) -> None:
    assert classify(meta(filename, mime)) == expected


def test_large_text_is_not_sniffed() -> None:
    assert classify(meta("bugreport.txt", size=50 * 1024 * 1024)) == "other"


def test_local_path_keeps_versions_apart_and_strips_directories(tmp_path: Path) -> None:
    assert local_path(tmp_path, meta("x.patch", jira_id="7")) == tmp_path / "7" / "x.patch"
    assert local_path(tmp_path, meta("../../etc/passwd", jira_id="8")) == tmp_path / "8" / "passwd"
    assert local_path(tmp_path, meta("..\\evil.patch", jira_id="9")) == tmp_path / "9" / "evil.patch"
    assert local_path(tmp_path, meta("..", jira_id="10")) == tmp_path / "10" / "attachment"


def test_format_patch_versions_and_embedded_image(client: DataCenterClient, tmp_path: Path) -> None:
    result = process(client, client.get_issue("PROJ-101"), tmp_path)
    image = next(p for p in result.values() if p.meta.filename == "screenshot.png")
    assert image.kind == "image"
    assert image.local_path is not None and image.local_path.read_bytes() == PNG

    patches = sorted((p for p in result.values() if p.kind == "diff"), key=lambda p: p.meta.created)
    assert [p.local_path.read_bytes() for p in patches if p.local_path] == [
        fixture_file("camera-fix.v1.patch"),
        fixture_file("camera-fix.v2.patch"),
    ]
    assert patches[0].local_path != patches[1].local_path
    assert patches[1].parsed == {
        "format": "format-patch",
        "files": [
            {
                "path": "services/camera/libcameraservice/CameraService.cpp",
                "old_path": None,
                "status": "modified",
                "added": 4,
                "removed": 0,
                "binary": False,
            },
            {
                "path": "services/camera/libcameraservice/CameraService.h",
                "old_path": None,
                "status": "modified",
                "added": 1,
                "removed": 1,
                "binary": False,
            },
        ],
    }
    assert all(p.sha256 and len(p.sha256) == 64 for p in [image, *patches])


def test_plain_diff_and_archive_not_downloaded(client: DataCenterClient, tmp_path: Path) -> None:
    result = process(client, client.get_issue("PROJ-102"), tmp_path)
    diff = next(p for p in result.values() if p.meta.filename == "settings.diff")
    archive = next(p for p in result.values() if p.meta.filename == "logs.zip")
    assert diff.kind == "diff" and diff.parsed is not None
    assert diff.parsed["format"] == "plain" and len(diff.parsed["files"]) == 4
    assert archive == ProcessedAttachment(archive.meta, "other")


def test_sniff_keeps_diff_named_txt_and_drops_other_text(client: DataCenterClient, tmp_path: Path) -> None:
    result = process(client, client.get_issue("PROJ-103"), tmp_path)
    fix = next(p for p in result.values() if p.meta.filename == "fix.txt")
    notes = next(p for p in result.values() if p.meta.filename == "notes.txt")
    assert fix.kind == "diff" and fix.local_path is not None and fix.local_path.exists()
    assert fix.parsed is not None and fix.parsed["files"][0]["path"] == "core/java/android/os/BatteryStats.java"
    assert notes == ProcessedAttachment(notes.meta, "other")
    assert not (tmp_path / "attachments" / notes.meta.jira_id).exists()


def test_binary_patch_attachment(client: DataCenterClient, tmp_path: Path) -> None:
    (patch,) = process(client, client.get_issue("PROJ-104"), tmp_path).values()
    assert patch.kind == "diff" and patch.parsed is not None
    assert [f["binary"] for f in patch.parsed["files"]] == [True, False]


def test_image_not_embedded_is_not_downloaded(client: DataCenterClient, fake_jira: FakeJiraStore, tmp_path: Path) -> None:
    fake_jira.attach("PROJ-102", "unrelated.png", PNG, "image/png", datetime(2026, 9, 24, tzinfo=UTC))
    result = process(client, client.get_issue("PROJ-102"), tmp_path)
    image = next(p for p in result.values() if p.meta.filename == "unrelated.png")
    assert image == ProcessedAttachment(image.meta, "image")


def test_image_embedded_in_comment_is_downloaded(client: DataCenterClient, fake_jira: FakeJiraStore, tmp_path: Path) -> None:
    shot = fake_jira.attach("PROJ-102", "repro.png", PNG, "image/png", datetime(2026, 9, 24, tzinfo=UTC))
    fake_jira.comment("PROJ-102", "Raj Patel", "!repro.png!", f'<p><img src="/secure/attachment/{shot.id}/repro.png" /></p>', datetime(2026, 9, 24, tzinfo=UTC))
    result = process(client, client.get_issue("PROJ-102"), tmp_path)
    assert result[f"repro.png@{shot.id}"].local_path is not None


def test_existing_file_is_reused(client: DataCenterClient, fake_jira: FakeJiraStore, tmp_path: Path) -> None:
    first = process(client, client.get_issue("PROJ-102"), tmp_path)
    downloads = [r for r in fake_jira.requests if r.startswith("GET /secure/attachment/")]
    second = process(client, client.get_issue("PROJ-102"), tmp_path)
    assert [r for r in fake_jira.requests if r.startswith("GET /secure/attachment/")] == downloads
    assert first == second


BASE = "https://jira.example.com"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/secure/attachment/10200/screenshot.png", "10200"),
        ("https://jira.example.com/secure/attachment/10200/a%20b.png", "10200"),
        ("/secure/thumbnail/10200/_thumb_10200.png", "10200"),
        ("https://other.example.com/secure/attachment/10200/x.png", None),
        ("/images/icons/emoticons/smile.png", None),
        ("data:image/png;base64,AAAA", None),
    ],
)
def test_attachment_id(url: str, expected: str | None) -> None:
    assert attachment_id(url, BASE) == expected


def test_attachment_id_with_context_path() -> None:
    assert attachment_id("/jira/secure/attachment/5/x.png", "https://host/jira") == "5"
    assert attachment_id("/secure/attachment/5/x.png", "https://host/jira") is None


def test_embedded_image_ids_only_counts_img_tags() -> None:
    bodies = [
        '<p><img src="/secure/attachment/1/a.png" /></p>',
        "<a href='/secure/attachment/2/fix.patch'>fix.patch</a>",
        "<span class=\"image-wrap\"><a href=\"/secure/attachment/3/b.png\"><img src='/secure/thumbnail/3/_thumb_3.png'></a></span>",
        '<img src="https://elsewhere.example.com/secure/attachment/4/c.png">',
    ]
    assert embedded_image_ids(bodies, BASE) == {"1", "3"}


def test_rewrite_points_downloaded_images_to_local_urls(client: DataCenterClient) -> None:
    issue = client.get_issue("PROJ-101")
    screenshot = next(a for a in issue.attachments if a.filename == "screenshot.png")
    local = f"/api/workspaces/1/attachments/{screenshot.jira_id}"
    html = rewrite_attachment_urls(issue.description_html, client.base_url, {screenshot.jira_id: local})
    assert f'<img src="{local}" style="border: 0px solid black" />' in html
    assert "/secure/attachment/" not in html
    # Everything else is untouched.
    assert html.replace(local, f"/secure/attachment/{screenshot.jira_id}/screenshot.png") == issue.description_html


def test_rewrite_leaves_unknown_and_foreign_urls() -> None:
    body = (
        "<a href='/secure/attachment/1/a.png'><img src=\"/secure/thumbnail/1/_thumb_1.png\"></a>"
        '<a href="/secure/attachment/2/fix.patch">fix</a>'
        '<img src="https://elsewhere.example.com/secure/attachment/1/a.png">'
    )
    assert rewrite_attachment_urls(body, BASE, {"1": "/local/1?x=1&y=2"}) == (
        "<a href='/local/1?x=1&amp;y=2'><img src=\"/local/1?x=1&amp;y=2\"></a>"
        '<a href="/secure/attachment/2/fix.patch">fix</a>'
        '<img src="https://elsewhere.example.com/secure/attachment/1/a.png">'
    )
