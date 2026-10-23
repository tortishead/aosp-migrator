from __future__ import annotations

import pytest

from patchmig.config import load_config
from patchmig.ledger import Cluster, InvalidTransition, LedgerStore, State
from patchmig.ledger.states import TRANSITIONS


@pytest.fixture
def store_and_run(write_config):
    store = LedgerStore.in_memory()
    run = store.create_run(load_config(write_config()), note="test")
    return store, run


def test_every_state_has_transition_rules():
    assert set(TRANSITIONS) == set(State)


def test_run_keeps_config_snapshot(store_and_run):
    store, run = store_and_run
    latest = store.latest_run()
    assert latest.id == run.id
    assert latest.config_snapshot["migration"]["label"] == "mig-A14-A15"
    assert len(latest.config_hash) == 64


def test_happy_path_is_recorded(store_and_run):
    store, run = store_and_run
    patch = store.add_patch(run, project="frameworks/base", subject="Add OEM power mode")
    for state in (State.NEEDED, State.RESOLVED, State.VERIFIED):
        patch = store.transition(patch, state, actor="agent:cluster_agent", reason=f"-> {state}")
    assert patch.state == State.VERIFIED
    history = store.history(patch)
    assert [(t.from_state, t.to_state) for t in history] == [
        (State.INGESTED, State.NEEDED),
        (State.NEEDED, State.RESOLVED),
        (State.RESOLVED, State.VERIFIED),
    ]
    assert store.patch_counts(run) == {State.VERIFIED: 1}


def test_invalid_transition_is_refused(store_and_run):
    store, run = store_and_run
    patch = store.add_patch(run, project="p", subject="s")
    with pytest.raises(InvalidTransition, match="cannot go from 'ingested' to 'verified'"):
        store.transition(patch, State.VERIFIED, actor="orchestrator")
    assert store.history(patch) == []


def test_only_humans_force_and_must_say_why(store_and_run):
    store, run = store_and_run
    patch = store.add_patch(run, project="p", subject="s")
    with pytest.raises(PermissionError):
        store.transition(patch, State.VERIFIED, actor="agent:x", reason="r", force=True)
    with pytest.raises(ValueError):
        store.transition(patch, State.VERIFIED, actor="human:xuan", force=True)
    patch = store.transition(
        patch, State.VERIFIED, actor="human:xuan", reason="verified manually", force=True
    )
    assert patch.state == State.VERIFIED
    assert store.history(patch)[0].forced


def test_stale_object_cannot_skip_state(store_and_run):
    store, run = store_and_run
    patch = store.add_patch(run, project="p", subject="s")
    store.transition(patch, State.UPSTREAM, actor="orchestrator")
    # `patch` still says INGESTED; the store must use the stored state (UPSTREAM).
    with pytest.raises(InvalidTransition):
        store.transition(patch, State.NEEDS_DECISION, actor="orchestrator")


def test_clusters_have_history_too(store_and_run):
    store, run = store_and_run
    with store.session() as s, s.begin():
        cluster = Cluster(run_id=run.id, name="power-mode")
        s.add(cluster)
    cluster = store.transition(cluster, State.NEEDED, actor="orchestrator")
    assert store.history(cluster)[0].entity_type == "cluster"


def test_ledger_file_persists(write_config, tmp_path):
    cfg = load_config(write_config())
    LedgerStore.open(cfg.ledger_path).create_run(cfg)
    assert LedgerStore.open(cfg.ledger_path).latest_run() is not None
