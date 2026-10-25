"""Synthetic tickets for the fake Jira. No real ticket data exists for tests.

PROJ-101..PROJ-125 match the JQL; PROJ-201 is only reachable as a linked ticket.

- PROJ-101: format-patch in two versions (same filename), embedded screenshot,
  HTML table and code block, two comments, relates to PROJ-201, blocks PROJ-102.
- PROJ-102: plain multi-file git diff (new file, rename), a zip archive.
- PROJ-103: diff named .txt (needs sniffing) and a non-diff .txt.
- PROJ-104: format-patch with a GIT binary patch.
- PROJ-105..PROJ-125: filler, no attachments, for pagination.
- PROJ-201: not in the JQL, has its own patch.
"""

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fake_jira.store import FakeIssue, FakeJiraStore

FILES = Path(__file__).parent / "files"

# 1x1 transparent PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=UTC)


def fixture_file(name: str) -> bytes:
    return (FILES / name).read_bytes()


def zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("logcat.txt", "09-20 10:00:00.000  1234  1234 E CameraService: client is null\n")
    return buffer.getvalue()


def default_store() -> FakeJiraStore:
    store = FakeJiraStore()

    camera = store.add(
        FakeIssue(
            key="PROJ-101",
            summary="Camera HAL crash on resume",
            status="In Progress",
            labels=["oem-patch", "camera"],
            components=["Camera"],
            fix_versions=["2026.Q4"],
            updated=at(25, 16),
        )
    )
    screenshot = store.attach("PROJ-101", "screenshot.png", PNG, "image/png", at(20))
    store.attach("PROJ-101", "0001-camera-fix-null-deref.patch", fixture_file("camera-fix.v1.patch"), "text/x-patch", at(21))
    store.attach("PROJ-101", "0001-camera-fix-null-deref.patch", fixture_file("camera-fix.v2.patch"), "text/x-patch", at(25))
    camera.description = (
        "h2. Steps\n# Open camera\n# Press home, reopen\n\n!screenshot.png!\n\n"
        "||Device||Build||\n|Pixel 8|AP2A.240905.003|\n\n{code:java}\nclient->notifyResume();\n{code}"
    )
    camera.description_html = (
        "<h2><a name=\"Steps\"></a>Steps</h2>\n<ol>\n\t<li>Open camera</li>\n\t<li>Press home, reopen</li>\n</ol>\n\n"
        f"<p><span class=\"image-wrap\" style=\"\"><img src=\"/secure/attachment/{screenshot.id}/screenshot.png\" style=\"border: 0px solid black\" /></span></p>\n\n"
        "<div class='table-wrap'>\n<table class='confluenceTable'><tbody>\n"
        "<tr>\n<th class='confluenceTh'>Device</th>\n<th class='confluenceTh'>Build</th>\n</tr>\n"
        "<tr>\n<td class='confluenceTd'>Pixel 8</td>\n<td class='confluenceTd'>AP2A.240905.003</td>\n</tr>\n"
        "</tbody></table>\n</div>\n\n"
        "<div class=\"code panel\" style=\"border-width: 1px;\"><div class=\"codeContent panelContent\">\n"
        "<pre class=\"code-java\">client-&gt;notifyResume();</pre>\n</div></div>"
    )
    store.comment(
        "PROJ-101",
        "Raj Patel",
        "Reproduced on *AP2A*. Log attached.",
        "<p>Reproduced on <b>AP2A</b>. Log attached.</p>",
        at(22),
    )
    store.comment(
        "PROJ-101",
        "Jane Doe",
        "Uploaded v2 of the patch, see {{CameraService.h}}.",
        "<p>Uploaded v2 of the patch, see <tt>CameraService.h</tt>.</p>",
        at(25, 16),
    )

    store.add(
        FakeIssue(
            key="PROJ-102",
            summary="Settings brightness summary shows values above 100%",
            description="Clamp the brightness summary.",
            description_html="<p>Clamp the brightness summary.</p>",
            labels=["oem-patch"],
            components=["Settings"],
            fix_versions=["2026.Q4"],
            updated=at(24),
        )
    )
    store.attach("PROJ-102", "settings.diff", fixture_file("settings.diff"), "text/x-diff", at(23))
    store.attach("PROJ-102", "logs.zip", zip_bytes(), "application/zip", at(23))

    store.add(
        FakeIssue(
            key="PROJ-103",
            summary="Battery history gap after long screen-off",
            description="Patch attached as fix.txt.",
            description_html="<p>Patch attached as fix.txt.</p>",
            labels=["oem-patch", "power"],
            components=["Framework"],
            updated=at(23),
        )
    )
    store.attach("PROJ-103", "fix.txt", fixture_file("fix.txt"), "text/plain", at(22))
    store.attach("PROJ-103", "notes.txt", fixture_file("notes.txt"), "text/plain", at(22))

    store.add(
        FakeIssue(
            key="PROJ-104",
            summary="Replace OEM battery icon",
            status="Resolved",
            labels=["oem-patch", "systemui"],
            components=["SystemUI"],
            fix_versions=["2026.Q3"],
            updated=at(29),
        )
    )
    store.attach("PROJ-104", "icons.patch", fixture_file("icons.patch"), "text/x-patch", at(29))

    for n in range(105, 126):
        store.add(
            FakeIssue(
                key=f"PROJ-{n}",
                summary=f"Filler ticket {n}",
                description=f"Filler description {n}.",
                description_html=f"<p>Filler description {n}.</p>",
                components=["Framework"],
                updated=at(1) + timedelta(hours=n),
            )
        )

    store.add(
        FakeIssue(
            key="PROJ-201",
            summary="Audio HAL callback after stream release",
            description="Upstream fix this camera issue depends on.",
            description_html="<p>Upstream fix this camera issue depends on.</p>",
            status="Closed",
            components=["Audio"],
            updated=at(18),
            in_jql=False,
        )
    )
    store.attach("PROJ-201", "audio-guard.patch", fixture_file("audio-linked.patch"), "text/x-patch", at(17))

    store.link("PROJ-101", "Relates", "PROJ-201")
    store.link("PROJ-101", "Blocks", "PROJ-102")
    return store
