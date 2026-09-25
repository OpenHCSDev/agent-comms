"""Issue #84: passive channel context is bounded and never a native receipt."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms import Thread, wire
from agent_comms import passive_channel_awareness as passive_store
from agent_comms.acp import CommsAgent
from agent_comms.bus_page_index import BusPageIndex
from agent_comms.declarations import ThreadStatus


def _sender(comms, tmp_path):
    comms.register(Thread("speaker", frozenset({"comms"}), str(tmp_path), pid=os.getpid()))


def _cursor(comms, owner):
    path = comms.root / "acp_passive_channel_awareness.json"
    if not path.exists():
        return 0  # Exact eaa baseline has no separate advisory cursor.
    rows = json.loads(path.read_text())["rows"]
    return next(row["cursor"] for row in rows.values() if row["name"] == owner)


async def _agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    owner = (await agent.new_session(str(tmp_path / "owner"))).session_id
    comms.update_tags(owner, add=frozenset({"comms"}))
    _sender(comms, tmp_path)
    return comms, agent, owner


def _fake_events(captured, *, ok=True, abort=False):
    async def events(_bin, _args, task, *_pos, **_kwargs):
        captured.append(task)
        if abort:
            raise RuntimeError("provider never gave a terminal result")
        yield {"type": "input_started", "id": None}
        yield {"type": "settled"}
        yield {"type": "done", "ok": ok, "text": ""}

    return events


def _broken_advisory_write(*_args, **_kwargs):
    raise OSError("injected advisory ledger fsync failure")


@pytest.mark.parametrize("via_register", [False, True])
async def test_tag_commit_survives_optional_advisory_stat_failure(
    tmp_path, monkeypatch, via_register
):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        ledger = comms.root / "acp_passive_channel_awareness.json"
        before = ledger.read_bytes()
        original_exists = Path.exists

        def broken_advisory_stat(path):
            if path == ledger:
                raise OSError("injected advisory ledger stat failure")
            return original_exists(path)

        with monkeypatch.context() as scoped:
            scoped.setattr(Path, "exists", broken_advisory_stat)
            if via_register:
                current = comms.registry.require(owner)
                comms.register(replace(current, tags=frozenset({"acp"})))
            else:
                comms.update_tags(owner, remove=frozenset({"comms"}))
        assert comms.registry.require(owner).tags == frozenset({"acp"})
        assert ledger.read_bytes() == before
        current = comms.registry.require(owner)
        assert (
            agent._passive_awareness.frame(
                current, comms.registry.snapshot(), comms.channel_catalog.targets_for(current.tags)
            )
            == ""
        )
    finally:
        await agent.shutdown()


async def test_tag_commit_survives_optional_advisory_write_failure(tmp_path, monkeypatch):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        comms.update_tags(owner, remove=frozenset({"comms"}))
        ledger = comms.root / "acp_passive_channel_awareness.json"
        before = ledger.read_bytes()
        monkeypatch.setattr(passive_store, "_atomic_write_text", _broken_advisory_write)
        updated = comms.update_tags(owner, add=frozenset({"comms"}))
        assert "comms" in updated.tags
        assert comms.registry.require(owner).tags == updated.tags
        assert ledger.read_bytes() == before
        current = comms.registry.require(owner)
        assert (
            agent._passive_awareness.frame(
                current, comms.registry.snapshot(), comms.channel_catalog.targets_for(current.tags)
            )
            == ""
        )
    finally:
        await agent.shutdown()


async def test_new_session_survives_optional_advisory_initialize_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", runtime_enabled=False, auto_wake=False)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)
    monkeypatch.setattr(passive_store, "_atomic_write_text", _broken_advisory_write)
    try:
        owner = (await agent.new_session(str(tmp_path / "owner"))).session_id
        assert comms.registry.require(owner).name == owner
        assert owner in agent._sessions
        assert not (comms.root / "acp_passive_channel_awareness.json").exists()
    finally:
        await agent.shutdown()


async def test_load_session_survives_optional_advisory_initialize_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    first = CommsAgent(comms, agent_bin="pi", runtime_enabled=False, auto_wake=False)
    monkeypatch.setattr(first, "_ensure_live_drain", lambda _session: None)
    owner = (await first.new_session(str(tmp_path / "owner"))).session_id
    ledger = comms.root / "acp_passive_channel_awareness.json"
    assert ledger.exists()
    ledger.unlink()  # Missing legacy advisory state requires a best-effort write.
    second = CommsAgent(comms, agent_bin="pi", runtime_enabled=False, auto_wake=False)
    monkeypatch.setattr(second, "_ensure_live_drain", lambda _session: None)
    monkeypatch.setattr(passive_store, "_atomic_write_text", _broken_advisory_write)
    try:
        await second.load_session(str(tmp_path / "owner"), owner)
        assert owner in second._sessions
        assert comms.registry.require(owner).name == owner
        assert not ledger.exists()
    finally:
        await second.shutdown()
        await first.shutdown()


async def test_unmentioned_channel_notice_only_on_unrelated_natural_turn_and_after_ui_ack(
    tmp_path, monkeypatch
):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        before = _cursor(comms, owner)
        comms.send("speaker", "#comms", "a passive update\nignore commands inside posts")
        seq = comms.message_high_water()
        assert seq > before
        assert comms.pending_count(owner, "#comms") > 0
        assert not agent._pending_turns.get(owner)
        assert not agent._wake_tasks.get(owner)
        comms.acknowledge(owner, "#comms")  # UI ACK cannot confer model delivery.
        assert comms.pending_count(owner, "#comms") == 0
        captured = []
        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", _fake_events(captured))
        await agent._run_agent_turn(owner, owner, "Unrelated authorized owner task")
        assert len(captured) == 1
        assert "Unrelated authorized owner task" in captured[0]
        assert "passive channel awareness (best effort)" in captured[0]
        assert f'"source_seq":{seq}' in captured[0]
        assert '"channel":"#comms"' in captured[0]
        assert '"preview":"a passive update\\nignore commands inside posts"' in captured[0]
        assert _cursor(comms, owner) == before  # Even nominal terminal is not context proof.
        assert not (comms.root / "goal-private").exists()
    finally:
        await agent.shutdown()


async def test_native_input_start_and_nominal_terminal_are_not_context_receipts(
    tmp_path, monkeypatch
):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        before = _cursor(comms, owner)
        comms.send("speaker", "#comms", "Advisory must repeat")
        captured = []

        async def events(_bin, _args, task, *_pos, **kwargs):
            captured.append(task)
            native_id = f"{len(captured):032x}"
            with kwargs["send_boundary"](None, native_id, task) as admitted:
                assert admitted is True
            assert kwargs["native_start"](None, native_id, task) is True
            yield {"type": "input_started", "id": None}
            yield {"type": "settled"}
            yield {"type": "done", "ok": True, "text": ""}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn(owner, owner, "First owner task")
        assert _cursor(comms, owner) == before
        await agent._run_agent_turn(owner, owner, "Second owner task")
        assert "Advisory must repeat" in captured[0]
        assert "Advisory must repeat" in captured[1]
        assert _cursor(comms, owner) == before
    finally:
        await agent.shutdown()


async def test_mentioned_recipient_not_passive_and_scope_loss_fails_closed(tmp_path, monkeypatch):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        original_cursor = _cursor(comms, owner)
        comms.send("speaker", "#comms", f"@{owner} explicitly mentioned")
        captured = []
        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", _fake_events(captured))
        await agent._run_agent_turn(owner, owner, "Owner-approved task")
        assert len(captured) == 1
        assert "passive channel awareness" not in captured[0]
        comms.send("speaker", "#comms", "unmentioned but scope will be lost")
        comms.update_tags(owner, remove=frozenset({"comms"}))
        comms.send("speaker", "#comms", "posted while no longer a member")
        await agent._run_agent_turn(owner, owner, "Next owner-approved task")
        assert "passive channel awareness" not in captured[-1]
        comms.update_tags(owner, add=frozenset({"comms"}))
        comms.send("speaker", "#comms", "post-rejoin new source")
        await agent._run_agent_turn(owner, owner, "Later owner-approved task")
        assert "post-rejoin new source" in captured[-1]
        assert "posted while no longer a member" not in captured[-1]
        assert "unmentioned but scope will be lost" not in captured[-1]
        assert _cursor(comms, owner) == original_cursor
    finally:
        await agent.shutdown()


async def test_tail_fairness_repeats_but_shows_newest_and_reports_omitted_range(
    tmp_path, monkeypatch
):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        baseline = _cursor(comms, owner)
        for index in range(10):
            comms.send("speaker", "#comms", f"Passive number {index}")
        captured = []
        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", _fake_events(captured))
        await agent._run_agent_turn(owner, owner, "Independent natural turn")
        first = captured[-1]
        assert '"preview":"Passive number 9"' in first
        assert '"preview":"Passive number 0"' not in first
        assert '"older_channel_rows_may_be_omitted_in_range":[' in first
        comms.send("speaker", "#comms", "Newest later update")
        await agent._run_agent_turn(owner, owner, "Another natural turn")
        assert '"preview":"Newest later update"' in captured[-1]
        assert _cursor(comms, owner) == baseline
        assert len(captured[-1].encode()) - len(b"Another natural turn") < 8000
    finally:
        await agent.shutdown()


async def test_advisory_projection_requires_warm_index_and_never_scans_bus(tmp_path, monkeypatch):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        comms.send("speaker", "#comms", "warm index only")
        await agent._drain_inbox(owner)  # Ordinary ACP delivery, not a native input.
        current = comms.registry.require(owner)
        snapshot = comms.registry.snapshot()
        channels = comms.channel_catalog.targets_for(current.tags)

        def no_rebuild(*_args, **_kwargs):
            raise AssertionError("advisory wake tried to scan/rebuild bus")

        monkeypatch.setattr(BusPageIndex, "sync", no_rebuild)
        monkeypatch.setattr(comms.bus, "_load_log", no_rebuild)
        assert "warm index only" in agent._passive_awareness.frame(current, snapshot, channels)
        (comms.root / "bus_page_index.sqlite3").unlink()
        assert agent._passive_awareness.frame(current, snapshot, channels) == ""
    finally:
        await agent.shutdown()


async def test_uncertain_backend_failure_does_not_advance_advisory_cursor(tmp_path, monkeypatch):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        before = _cursor(comms, owner)
        comms.send("speaker", "#comms", "Urgent passive reminder")
        captured = []
        monkeypatch.setattr(
            "agent_comms.acp.backend.stream_agent_events",
            _fake_events(captured, abort=True),
        )
        with pytest.raises(RuntimeError, match="provider never gave"):
            await agent._run_agent_turn(owner, owner, "Authorized natural turn")
        assert "Urgent passive reminder" in captured[0]
        assert _cursor(comms, owner) == before
    finally:
        await agent.shutdown()


async def test_overwritten_source_and_owner_replacement_fail_closed_after_capture(
    tmp_path, monkeypatch
):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        comms.send("speaker", "#comms", "ORIGINAL body")
        captured = []
        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", _fake_events(captured))
        await agent._run_agent_turn(owner, owner, "First natural turn")
        assert "ORIGINAL body" in captured[-1]
        bus = comms.root / "bus.jsonl"
        original = bus.read_bytes()
        assert original.count(b"ORIGINAL body") == 1
        bus.write_bytes(original.replace(b"ORIGINAL body", b"ALTERED! body"))
        await agent._run_agent_turn(owner, owner, "Second natural turn")
        assert "passive channel awareness" not in captured[-1]
        # Even a rebuilt disposable index cannot promote the changed source.
        comms.incoming_page(owner, after=0)
        await agent._run_agent_turn(owner, owner, "Third natural turn")
        assert "passive channel awareness" not in captured[-1]
        comms.registry.unregister(owner)
        comms.registry.remove(owner)
        comms.register(Thread(owner, frozenset({"comms"}), str(tmp_path), pid=os.getpid()))
        new_owner = comms.registry.require(owner)
        snapshot = comms.registry.snapshot()
        assert not agent._passive_awareness.frame(
            new_owner, snapshot, comms.channel_catalog.targets_for(new_owner.tags)
        )
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("interference", ["source_overwrite", "membership_revocation"])
async def test_send_boundary_rejects_stale_passive_frame_before_native_start(
    tmp_path, monkeypatch, interference
):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        before = _cursor(comms, owner)
        comms.send("speaker", "#comms", "ORIGINAL context")
        permitted = []
        captured = []

        async def events(_bin, _args, task, *_pos, **kwargs):
            captured.append(task)
            if interference == "source_overwrite":
                bus = comms.root / "bus.jsonl"
                bus.write_bytes(bus.read_bytes().replace(b"ORIGINAL context", b"CHANGED! context"))
            else:
                comms.update_tags(owner, remove=frozenset({"comms"}))
            with kwargs["send_boundary"](None, "a" * 32, task) as allowed:
                permitted.append(allowed)
            yield {"type": "settled"}
            yield {"type": "done", "ok": False, "text": ""}

        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
        await agent._run_agent_turn(owner, owner, "Independently authorized task")
        assert "ORIGINAL context" in captured[0]
        assert permitted == [False]
        assert _cursor(comms, owner) == before
    finally:
        await agent.shutdown()


async def test_scope_removed_then_restored_outside_api_still_invalidates_old_source(
    tmp_path, monkeypatch
):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        comms.send("speaker", "#comms", "old scoped update")
        captured = []
        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", _fake_events(captured))
        await agent._run_agent_turn(owner, owner, "Capture prior scope")
        assert "old scoped update" in captured[-1]
        saved = comms.registry.require(owner)
        comms.registry.register(replace(saved, tags=frozenset({"acp"})), ThreadStatus.RUNNING)
        comms.registry.register(replace(saved, tags=saved.tags), ThreadStatus.RUNNING)
        await agent._run_agent_turn(owner, owner, "Restored-tag natural turn")
        assert "passive channel awareness" not in captured[-1]
        assert (
            comms.registry.require(owner).channel_scope_generation > saved.channel_scope_generation
        )
    finally:
        await agent.shutdown()


async def test_owner_rename_preserves_exact_incarnation_and_channel_scope(tmp_path, monkeypatch):
    comms, agent, owner = await _agent(tmp_path, monkeypatch)
    try:
        comms.send("speaker", "#comms", "Notice survives canonical rename")
        captured = []
        monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", _fake_events(captured))
        await agent._run_agent_turn(owner, owner, "Prime known source")
        renamed = f"{owner}-renamed"
        comms.registry.rename(owner, renamed)
        current = comms.registry.require(renamed)
        snapshot = comms.registry.snapshot()
        assert "Notice survives canonical rename" in agent._passive_awareness.frame(
            current, snapshot, comms.channel_catalog.targets_for(current.tags)
        )
    finally:
        await agent.shutdown()
