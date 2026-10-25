"""One confidence scale: in every agent brief, in the CLI and PR texts, and in the front end."""

from __future__ import annotations

import json
from html import unescape
from pathlib import Path

from patchmig.agents.cluster import ClusterAnswer, FixAnswer, ReviewAnswer
from patchmig.agents.intent import IntentResult, render_intent
from patchmig.confidence import SCALE, describe, level, rubric, scale
from patchmig.web.api import index_html
from tests.test_ingest import make_config  # noqa: F401  (fixture)
from tests.test_web_actions import served, start  # noqa: F401  (fixture)


def test_levels_cover_zero_to_one_without_gaps():
    rows = scale()
    assert [r["name"] for r in rows] == ["certain", "confident", "unsure", "weak", "guess"]
    assert rows[0]["to"] == 1.0 and rows[-1]["from"] == 0.0
    for upper, lower in zip(rows, rows[1:], strict=False):
        assert round(upper["from"] - 0.01, 2) == lower["to"]
    assert level(None) is None and describe(None) == "–"
    assert describe(0.9) == "0.90 (certain)" and describe(0.85) == "0.85 (confident)"
    assert describe(0.899) == "0.90 (certain)"  # the level of the number shown
    assert level(0.5).name == "unsure" and level(0.49).name == "weak" and level(0).name == "guess"


def test_every_agent_that_rates_itself_gets_the_scale():
    from patchmig.agents import cluster, intent

    text = rubric("the intent")
    for lv in SCALE:
        assert f"{lv.name}: {lv.meaning}" in text
    # Every brief whose answer carries a confidence ends with the rubric.
    for module, models in ((intent, [IntentResult]), (cluster, [ClusterAnswer, ReviewAnswer,
                                                                FixAnswer])):  # fmt: skip
        assert all("confidence" in m.model_fields for m in models)
        assert Path(module.__file__).read_text().count("+ rubric(") == len(models)
    result = IntentResult(problem="p", change="c", verification="v", components=[],
                          sources=["diff"], confidence=0.6)  # fmt: skip
    assert "(confidence 0.60 (unsure))" in render_intent(result)


def test_front_end_gets_the_scale_and_the_ratings(served):  # noqa: F811
    client, jobs, _, _, _, fake = served
    page = client.get("/").text
    raw = page.split('data-confidence="')[1].split('"')[0]
    assert json.loads(unescape(raw)) == scale()
    assert 'data-confidence="' in index_html(static=True)
    job = start(client, jobs, ["ingest", "analyse", "intents"], {"ingest": {"use_jira": False}})
    run = job["run_id"]
    patches = client.get(f"/api/runs/{run}/patches").json()
    rated = [p for p in patches if p["intent_confidence"] is not None]
    assert rated and all(p["intent_confidence"] == 0.8 for p in rated)  # the fake intent writer
    assert all("## Confidence scale" in task.brief for task, _ in fake.calls)
    assert all("confidence" in c for c in client.get(f"/api/runs/{run}/clusters").json())
