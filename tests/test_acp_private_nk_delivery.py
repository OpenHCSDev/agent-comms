"""Explicit ACP session owner consumes ordinary private N/K without legacy ACK.

Only model responses are faked; this does not prove provider delivery or
activate private processing for existing public sessions.
"""

from __future__ import annotations

import os

import pytest

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.acp import CommsAgent
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import MutationStore, PublicationActivationBlocked
from agent_comms.declarations import Thread
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.operations import Comms
from agent_comms.tools import invoke_tool
from test_coordinated_runtime import _fake_model
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


def _session(tmp_path, *, package=True):
    root = tmp_path / "wire"
    comms = Comms(root)
    comms.register(Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()))
    owner = Thread("beta", frozenset({"team"}), str(tmp_path), pid=os.getpid())
    comms.register(owner)
    root_id = comms.initialize_private_initial_protocol()
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
        store.register_participant(
            stable_thread_lookup(owner.created_at), "beta", "beta", committed=True
        )
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=tmp_path if package else None,
        private_nk_wire_root_id=root_id if package else None,
    )
    agent._sessions["beta"] = "beta"
    agent._session_titles["beta"] = "beta"
    agent._session_worktrees["beta"] = str(tmp_path)
    return comms, agent, root_id


async def test_acp_new_session_owner_consumes_private_selected_source(tmp_path, monkeypatch):
    root = tmp_path / "wire"
    project = tmp_path / "proj"
    project.mkdir()
    comms = Comms(root)
    comms.register(Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()))
    root_id = comms.initialize_private_initial_protocol()
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=tmp_path,
        private_nk_wire_root_id=root_id,
    )

    async def noop(*args, **kwargs):
        return None

    async def no_options(*args, **kwargs):
        return []

    monkeypatch.setattr(agent._runtime, "start", noop)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _: None)
    monkeypatch.setattr(agent, "_config_options", no_options)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    session = await agent.new_session(cwd=str(project), mcp_servers=[])
    owner = comms.registry.require(session.session_id)
    with MutationStore(str(root / "coordination.sqlite3")) as store:
        store.register_participant(
            stable_thread_lookup(owner.created_at), owner.name, owner.name, committed=True
        )
    invoke_tool(
        comms,
        "comms_send",
        {"from": "sender", "to": owner.name, "body": "Compute 17+25"},
    )
    assert await agent._drain_inbox(session.session_id) == 1
    assert len(calls) == 1
    assert agent._inbox_cursors[session.session_id] == 0


async def test_acp_session_selected_native_pipeline_never_uses_legacy_ack(tmp_path, monkeypatch):
    comms, agent, root_id = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    sent = invoke_tool(
        comms, "comms_send", {"from": "sender", "to": "beta", "body": "Compute 17+25"}
    )
    original = comms.bus.message_by_id(sent["id"])
    assert await agent._drain_inbox("beta") == 1
    assert len(calls) == 1
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_inputs WHERE session_id IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
                (root_id, original.seq),
            ).fetchone()[0]
            == 1
        )
    assert await agent._drain_inbox("beta") == 0
    assert len(calls) == 1
    assert agent._inbox_cursors == {} and agent._pending_turns == {}
    assert not (comms.root / "acks.json").exists()


async def test_acp_private_does_not_overlap_owner_turn(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    agent._active_turns["beta"] = "active-human-turn"
    assert await agent._drain_inbox("beta") == 0
    assert calls == [] and agent._inbox_cursors == {}
    agent._active_turns.pop("beta")
    assert await agent._drain_inbox("beta") == 1
    assert len(calls) == 1


async def test_acp_private_without_explicit_package_refuses_legacy_delivery(tmp_path):
    comms, agent, root_id = _session(tmp_path, package=False)
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    original = comms.bus.message_by_id(sent["id"])
    with pytest.raises(PublicationActivationBlocked, match="explicit matching root"):
        await agent._drain_inbox("beta")
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
                (root_id, original.seq),
            ).fetchone()[0]
            == 0
        )
    assert agent._inbox_cursors == {} and agent._pending_turns == {}


async def test_acp_private_no_wake_has_delivery_receipt_but_no_model(tmp_path, monkeypatch):
    comms, agent, root_id = _session(tmp_path)
    alpha = Thread("alpha", frozenset({"team"}), str(tmp_path), pid=os.getpid())
    comms.register(alpha)
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        store.register_participant(
            stable_thread_lookup(alpha.created_at), "alpha", "alpha", committed=True
        )
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    sent = invoke_tool(
        comms,
        "comms_send",
        {"from": "sender", "to": "#team", "body": "@alpha review this."},
    )
    original = comms.bus.message_by_id(sent["id"])
    assert await agent._drain_inbox("beta") == 0
    assert calls == [] and agent._inbox_cursors == {}
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        receipt = store._connection.execute(
            "SELECT kind,claim_id FROM cohort_delivery_receipts WHERE wire_root_id=? "
            "AND wire_seq=? AND recipient_lookup=?",
            (
                root_id,
                original.seq,
                stable_thread_lookup(comms.registry.require("beta").created_at),
            ),
        ).fetchone()
        assert tuple(receipt) == ("unmentioned_observer", None)


async def test_acp_uncertain_native_turn_is_not_replayed_or_acked(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(fail_on=1)
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    assert comms.bus.message_by_id(sent["id"]) is not None
    with pytest.raises(NativePiUnavailable):
        await agent._drain_inbox("beta")
    assert len(calls) == 1
    assert await agent._drain_inbox("beta") == 0
    assert len(calls) == 1
    assert agent._inbox_cursors == {} and agent._pending_turns == {}
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        row = store._connection.execute("SELECT session_id FROM native_runtime_inputs").fetchone()
        assert row is not None and row[0] is None


async def test_acp_mismatched_root_and_bad_package_cannot_accept_claim(tmp_path, monkeypatch):
    comms, agent, root_id = _session(tmp_path)
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    original = comms.bus.message_by_id(sent["id"])
    agent._private_nk_wire_root_id = "f" * 32
    with pytest.raises(PublicationActivationBlocked, match="explicit matching root"):
        await agent._drain_inbox("beta")
    agent._private_nk_wire_root_id = root_id
    # Preflight must run before any SQL acceptance, not only before reservation.
    monkeypatch.setattr(
        cohort_foreground,
        "_trusted_package",
        lambda _: (_ for _ in ()).throw(PublicationActivationBlocked("unreviewed Pi")),
    )
    with pytest.raises(PublicationActivationBlocked, match="unreviewed Pi"):
        await agent._drain_inbox("beta")
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
                (root_id, original.seq),
            ).fetchone()[0]
            == 0
        )
    assert agent._inbox_cursors == {}
