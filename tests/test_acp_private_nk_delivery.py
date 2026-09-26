"""Explicit ACP session owner consumes ordinary private N/K without legacy ACK.

Only model responses are faked; this does not prove provider delivery or
activate private processing for existing public sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.acp import CommsAgent
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import MutationStore, PublicationActivationBlocked, StaleFence
from agent_comms.declarations import Thread
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_prompt_binding import install_prompt_binding_schema
from agent_comms.operations import Comms
from agent_comms.tools import invoke_tool
from test_coordinated_runtime import _fake_model
from test_coordinated_runtime import tmp_path as private_root_fixture
from test_native_prompt_binding import _fake_model as separate_session_fake

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


def test_cursor_v1_event_order_fixture_is_consistent():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "private_native_cursor_v1.json").read_text()
    )
    current = fixture["trustedLoad"]["cursor"]
    assert current["status"] == "proven" and current["scope"]["sessionId"] == "beta"
    quarantined = False

    def apply(proposed, current, quarantined):
        scope, bound = proposed["scope"], current["scope"]
        assert 1 <= proposed["revision"] < 2**53
        if quarantined:
            return "reject_quarantined", current, True
        same_attachment = all(
            scope[key] == bound[key] for key in ("sessionId", "wireRootId", "ownerThread")
        )
        if not same_attachment:
            return "reject_foreign_scope", current, False
        if scope["ownerCreatedAt"] != bound["ownerCreatedAt"]:
            return "quarantine_ambiguous_incarnation", current, True
        if scope["ownerEpoch"] > bound["ownerEpoch"]:
            return "quarantine_newer_epoch", current, True
        if scope["ownerEpoch"] < bound["ownerEpoch"]:
            return "reject_stale_scope", current, False
        if scope["ownerPid"] != bound["ownerPid"]:
            return "quarantine_ambiguous_incarnation", current, True
        if proposed["revision"] == current["revision"] and proposed != current:
            return "reject_equal_revision_conflict", current, False
        if proposed["revision"] > current["revision"]:
            return "accept", proposed, False
        return "reject_stale_revision", current, False

    for entry in fixture["updates"]:
        decision, current, quarantined = apply(entry["cursor"], current, quarantined)
        assert decision == entry["decision"]
    assert fixture["autoReconnectReadyForwardedToClient"] is False
    assert quarantined and fixture["expectedBeforeNextLoad"] == {
        "status": "unavailable",
        "quarantined": True,
        "ownerEpoch": current["scope"]["ownerEpoch"],
    }
    # Only the explicit trusted load resets quarantine; the proxy's private
    # ready is not forwarded to this client after automatic reconnect.
    current = fixture["nextTrustedLoad"]["cursor"]
    quarantined = False
    assert current["status"] == "none" and current["scope"]["ownerEpoch"] == 4
    for entry in fixture["afterNextLoad"]:
        decision, current, quarantined = apply(entry["cursor"], current, quarantined)
        assert decision == entry["decision"]
    assert not quarantined
    assert (current["status"], current["revision"], current["scope"]["ownerEpoch"]) == (
        fixture["expectedAfterNextLoad"]["status"],
        fixture["expectedAfterNextLoad"]["revision"],
        fixture["expectedAfterNextLoad"]["ownerEpoch"],
    )


def test_cursor_v1_prebind_newer_callback_poison_floor():
    """A delayed trusted old response cannot erase earlier callback evidence."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "private_native_cursor_v1.json").read_text()
    )
    race = fixture["prebindRace"]
    pending = [race["callbackBeforeTrustedResult"]]
    assert len(pending) <= race["bufferBound"]
    old = race["delayedTrustedLoad"]
    matching = [
        update
        for update in pending
        if isinstance(update.get("scope"), dict)
        and all(
            update["scope"][key] == old["scope"][key]
            for key in ("sessionId", "wireRootId", "ownerThread", "ownerCreatedAt")
        )
    ]
    floor = max(update["scope"]["ownerEpoch"] for update in matching)
    visible = old if old["scope"]["ownerEpoch"] >= floor else None
    assert visible is None and race["expectedAfterDelayedLoad"] == {
        "status": "unavailable",
        "quarantined": True,
        "ownerEpochFloor": floor,
    }
    fresh = race["subsequentTrustedLoad"]
    assert fresh["scope"]["ownerEpoch"] >= floor
    visible = fresh
    # The buffered same-epoch callback is older than the trusted revision;
    # it cannot overwrite the subsequent trusted response.
    assert matching[0]["revision"] < fresh["revision"]
    assert (visible["status"], visible["scope"]["ownerEpoch"]) == (
        race["expectedAfterSubsequentLoad"]["status"],
        race["expectedAfterSubsequentLoad"]["ownerEpoch"],
    )
    assert race["overflow"]["received"] > race["bufferBound"]
    assert race["overflow"]["status"] == "unavailable"
    assert race["overflow"]["clearOnlyAfter"] == (
        "subsequent_explicit_load_initiated_after_overflow"
    )


def test_cursor_v1_null_scope_prebind_hides_delayed_old_proof():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "private_native_cursor_v1.json").read_text()
    )
    race = fixture["nullScopePrebind"]
    pending = [race["callbackBeforeTrustedResult"]]
    assert pending[0]["scope"] is None and pending[0]["status"] == "unavailable"
    old = race["delayedTrustedLoad"]
    assert old["status"] == "proven" and old["revision"] < pending[0]["revision"]
    # Unknown owner is a receiving-attachment poison, not a comparable epoch.
    # Never dereference scope before checking it is a typed object.
    unknown_owner = any(update.get("scope") is None for update in pending)
    matching = [
        update
        for update in pending
        if isinstance(update.get("scope"), dict)
        and update["scope"]["sessionId"] == old["scope"]["sessionId"]
    ]
    assert matching == [] and unknown_owner
    visible = None if unknown_owner else old
    assert visible is None and race["expectedAfterDelayedLoad"] == {
        "status": "unavailable",
        "quarantined": True,
        "reason": "unknown_owner",
    }
    # This is a separately initiated explicit load *after* the null event.
    fresh = race["subsequentExplicitLoadInitiatedAfterCallback"]
    assert fresh["scope"]["ownerEpoch"] > old["scope"]["ownerEpoch"]
    assert (fresh["status"], fresh["scope"]["ownerEpoch"]) == (
        race["expectedAfterSubsequentLoad"]["status"],
        race["expectedAfterSubsequentLoad"]["ownerEpoch"],
    )
    assert race["expectedAfterSubsequentLoad"]["quarantined"] is False


def test_cursor_v1_distinct_key_saturation_is_attachment_sticky():
    """Illustrative contract reducer; mounted Toad acceptance is separate."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "private_native_cursor_v1.json").read_text()
    )
    race = fixture["distinctKeySaturation"]
    scope_fields = (
        "sessionId",
        "wireRootId",
        "ownerThread",
        "ownerCreatedAt",
        "ownerPid",
        "ownerEpoch",
    )

    def validate(row):
        assert row["version"] == 1 and type(row["revision"]) is int
        assert row["revision"] > 0 and row["status"] in {"none", "proven"}
        scope = row["scope"]
        assert all(name in scope for name in scope_fields)
        assert type(scope["ownerEpoch"]) is int and scope["ownerEpoch"] > 0
        assert len(scope["wireRootId"]) == 32
        assert all(c in "0123456789abcdef" for c in scope["wireRootId"])
        if row["status"] == "proven":
            required = (
                "wire_root_id",
                "recipient_lookup",
                "owner_thread",
                "owner_generation",
                "owner_admission_epoch",
                "covered_seq",
                "injected_seq",
                "input_id",
                "claim_id",
                "stage",
                "session_id",
                "request_generation",
            )
            assert all(name in row for name in required)
            assert row["wire_root_id"] == scope["wireRootId"]
            assert row["owner_thread"] == scope["ownerThread"]
            assert row["owner_admission_epoch"] == scope["ownerEpoch"]
        return row

    class ReferenceAttachment:
        def __init__(self):
            self.floors = {}
            self.evidence_loss = False
            self.visible = None

        def callback(self, row):
            scope = validate(row)["scope"]
            key = tuple(scope[name] for name in ("sessionId", "wireRootId", "ownerThread"))
            if self.evidence_loss:
                return
            if key not in self.floors and len(self.floors) >= race["floorCapacity"]:
                self.evidence_loss = True
                self.visible = None
                return
            self.floors[key] = max(self.floors.get(key, 0), scope["ownerEpoch"])

        def trusted_load(self, row):
            scope = validate(row)["scope"]
            if self.evidence_loss:
                return  # Same-Agent explicit load cannot repair a discarded floor.
            key = tuple(scope[name] for name in ("sessionId", "wireRootId", "ownerThread"))
            self.visible = row if scope["ownerEpoch"] >= self.floors.get(key, 0) else None

        def state(self):
            if self.evidence_loss:
                return {
                    "status": "unavailable",
                    "reason": "evidence_loss",
                    "attachmentSticky": True,
                }
            return {"status": self.visible["status"]} if self.visible else {"status": "unavailable"}

    attachment = ReferenceAttachment()
    template = validate(race["foreignCallbackTemplate"])
    low, high = race["foreignWireRootIdRangeInclusive"]
    callbacks = [
        {
            **template,
            "scope": {**template["scope"], "wireRootId": f"{i:032x}"},
            "revision": i,
        }
        for i in range(low, high + 1)
    ]
    for callback in callbacks:
        attachment.callback(callback)
    assert len(callbacks) == len(attachment.floors) == race["floorCapacity"] == 32
    real = validate(race["realOwnerCallback33"])
    old = validate(race["subsequentTrustedOldLoad"])
    assert real["scope"]["ownerEpoch"] > old["scope"]["ownerEpoch"]
    assert callbacks[-1]["revision"] < real["revision"]
    assert old["revision"] < real["revision"]
    attachment.callback(real)  # 33rd distinct key; never evict a retained floor.
    assert attachment.evidence_loss and len(attachment.floors) == 32
    attachment.trusted_load(old)  # delayed valid old proof must not revive
    assert attachment.state() == race["expectedAfter33rdAndSubsequentLoad"]
    attachment.trusted_load(race["laterTrustedSameAgentLoad"])
    assert attachment.state() == race["expectedAfter33rdAndSubsequentLoad"]
    screen_remount = attachment  # still the same Agent trust boundary
    assert screen_remount.state() == race["expectedAfter33rdAndSubsequentLoad"]
    fresh_agent = ReferenceAttachment()
    fresh_agent.trusted_load(race["freshAgentTrustedLoad"])
    assert fresh_agent.state() == {"status": "none"}
    assert race["clearOnlyAfter"] == (
        "fresh_agent_attachment_not_same_agent_explicit_load_or_screen_remount"
    )


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
    assert agent._inbox_cursors == {}  # private receipt, never legacy display cursor


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
    before = agent._session_metadata("beta")["agentComms"]["privateNativeCursor"]
    assert before["status"] == "none"
    assert before["version"] == 1 and before["revision"] >= 1
    assert before["scope"]["ownerThread"] == "beta"
    assert before["scope"]["wireRootId"] == root_id
    updates = []

    async def record_update(*, session_id, update):
        assert session_id == "beta"
        updates.append(update)

    monkeypatch.setattr(agent._runtime, "session_update", record_update)
    assert await agent._drain_inbox("beta") == 1
    assert len(calls) == 1
    current = agent._session_metadata("beta")["agentComms"]["privateNativeCursor"]
    assert current["status"] == "proven"
    assert current["covered_seq"] == original.seq
    assert current["injected_seq"] == original.seq
    announced = updates[-1].field_meta["agentComms"]["privateNativeCursor"]
    assert announced["scope"] == current["scope"]
    assert before["revision"] < announced["revision"] < current["revision"]
    assert announced["input_id"] == current["input_id"]
    assert updates[-1].field_meta["agentComms"]["lastSelectedCursorStatus"] == "proven"

    async def noop(*_args, **_kwargs):
        return None

    async def no_options(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent._runtime, "start", noop)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _: None)
    monkeypatch.setattr(agent, "_config_options", no_options)
    reconnected = await agent.load_session(str(tmp_path), "beta", mcp_servers=[])
    reconnect_cursor = reconnected.field_meta["agentComms"]["privateNativeCursor"]
    assert reconnect_cursor["scope"] == current["scope"]
    assert reconnect_cursor["revision"] > current["revision"]
    assert reconnect_cursor["input_id"] == current["input_id"]
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


async def test_delayed_old_cursor_update_cannot_rebind_new_owner_snapshot(tmp_path, monkeypatch):
    """ACP metadata orders a delayed epoch-2 update below trusted epoch-3 load.

    This tests the backend contract, not a Toad widget or provider acceptance.
    """
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    entered = asyncio.Event()
    release = asyncio.Event()
    delivered = []

    async def update(*, session_id, update):
        assert session_id == "beta"
        cursor = (update.field_meta or {}).get("agentComms", {}).get("privateNativeCursor")
        if cursor and cursor["status"] == "proven":
            entered.set()
            await release.wait()
        delivered.append(update)

    async def noop(*_args, **_kwargs):
        return None

    async def no_options(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent._runtime, "session_update", update)
    monkeypatch.setattr(agent._runtime, "start", noop)
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _: None)
    monkeypatch.setattr(agent, "_config_options", no_options)
    drain = asyncio.create_task(agent._drain_inbox("beta"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    comms.registry.unregister("beta")
    comms.registry.heartbeat("beta")
    loaded = await agent.load_session(str(tmp_path), "beta", mcp_servers=[])
    fresh = loaded.field_meta["agentComms"]["privateNativeCursor"]
    assert fresh["status"] == "none"
    assert fresh["scope"]["ownerEpoch"] > 0
    release.set()
    assert await asyncio.wait_for(drain, timeout=5) == 1
    old = delivered[-1].field_meta["agentComms"]["privateNativeCursor"]
    assert old["status"] == "proven" and old["input_id"]
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "private_native_cursor_v1.json").read_text()
    )
    assert set(old) == set(fixture["trustedLoad"]["cursor"])
    assert set(fresh) == set(fixture["nextTrustedLoad"]["cursor"])
    assert old["scope"]["ownerEpoch"] < fresh["scope"]["ownerEpoch"]
    assert old["revision"] < fresh["revision"]
    assert old["scope"]["sessionId"] == fresh["scope"]["sessionId"] == "beta"
    assert len(calls) == 1


async def test_observed_mid_session_admission_change_invalidates_old_proof(tmp_path, monkeypatch):
    """No reattach is required for a registry stop/heartbeat admission bump."""
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    updates = []

    async def record_update(*, session_id, update):
        assert session_id == "beta"
        cursor = (update.field_meta or {}).get("agentComms", {}).get("privateNativeCursor")
        if cursor is not None:
            updates.append(cursor)

    monkeypatch.setattr(agent._runtime, "session_update", record_update)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "first"})
    assert await agent._drain_inbox("beta") == 1
    proven = agent._session_metadata("beta")["agentComms"]["privateNativeCursor"]
    assert proven["status"] == "proven"
    comms.registry.unregister("beta")
    assert await agent._drain_inbox("beta") == 0
    assert updates[-1]["status"] == "unavailable" and updates[-1]["scope"] is None
    comms.registry.heartbeat("beta")
    assert await agent._drain_inbox("beta") == 0
    renewed = updates[-1]
    assert renewed["status"] == "none" and "input_id" not in renewed
    assert renewed["scope"]["ownerEpoch"] > proven["scope"]["ownerEpoch"]
    assert renewed["revision"] > proven["revision"]
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "second"})
    assert await agent._drain_inbox("beta") == 1
    assert updates[-1]["status"] == "none"
    assert updates[-1]["scope"] == renewed["scope"]
    assert len(calls) == 2  # no old-epoch native input can initialize new proof


async def test_unavailable_cursor_metadata_retains_owner_scope(tmp_path):
    comms, agent, _ = _session(tmp_path)
    before = agent._session_metadata("beta")["agentComms"]["privateNativeCursor"]
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        store._connection.execute("DROP TABLE native_runtime_schema_meta")
    unavailable = agent._session_metadata("beta")["agentComms"]["privateNativeCursor"]
    assert unavailable["status"] == "unavailable"
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "private_native_cursor_v1.json").read_text()
    )
    assert set(unavailable) == set(fixture["afterNextLoad"][1]["cursor"])
    assert unavailable["scope"] == before["scope"]
    assert unavailable["revision"] > before["revision"]
    assert "input_id" not in unavailable


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
    cursor = agent._session_metadata("beta")["agentComms"]["privateNativeCursor"]
    assert cursor["status"] == "coverage_only"
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "private_native_cursor_v1.json").read_text()
    )
    assert set(cursor) == set(fixture["coverageOnlyExample"])
    assert cursor["covered_seq"] == original.seq
    assert cursor["injected_seq"] == 0 and cursor["input_id"] is None
    assert await agent._drain_inbox("beta") == 0
    assert calls == []
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


async def test_two_acp_instances_cannot_engage_or_send_simultaneously(tmp_path, monkeypatch):
    comms, first, root_id = _session(tmp_path)
    second = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=tmp_path,
        private_nk_wire_root_id=root_id,
    )
    second._sessions["beta"] = "beta"
    second._session_titles["beta"] = "beta"
    second._session_worktrees["beta"] = str(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = separate_session_fake(decision="FULL")
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended(package, **kwargs):
        result = await fake(package, **kwargs)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", suspended)
    for body in ("one", "two"):
        invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": body})
    running = asyncio.create_task(first._drain_inbox("beta"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        with pytest.raises(StaleFence, match="busy"):
            await second._drain_inbox("beta")
        assert len(calls) == 1
        with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
            assert (
                store._connection.execute(
                    "SELECT COUNT(*) FROM wake_claims WHERE disposition='engaged'"
                ).fetchone()[0]
                == 1
            )
            assert (
                store._connection.execute("SELECT COUNT(*) FROM native_runtime_inputs").fetchone()[
                    0
                ]
                == 1
            )
    finally:
        release.set()
        assert await asyncio.wait_for(running, timeout=5) == 1
    assert await second._drain_inbox("beta") == 1
    assert len(calls) == 2
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM wake_claims WHERE disposition='engaged'"
            ).fetchone()[0]
            == 0
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM wake_claims WHERE disposition='completed'"
            ).fetchone()[0]
            == 2
        )


async def test_human_owner_turn_cannot_be_borrowed_by_private_acp(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    comms.begin_turn("beta", "human-live-turn")
    with pytest.raises(StaleFence, match="busy"):
        await agent._drain_inbox("beta")
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store._connection.execute("SELECT COUNT(*) FROM claim_batch_receipts").fetchone()[0]
            == 0
        )
    assert calls == []
    comms.finish_turn("beta", "human-live-turn")
    assert await agent._drain_inbox("beta") == 1
    assert len(calls) == 1


async def test_goal_change_between_reservation_and_native_send_refuses(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended_before_send(package, **kwargs):
        entered.set()
        await release.wait()
        return await fake(package, **kwargs)

    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", suspended_before_send)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    running = asyncio.create_task(agent._drain_inbox("beta"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        goal = comms.update_goal("beta", "set", text="Work on a separate task")
        assert goal.active
    finally:
        release.set()
    with pytest.raises(StaleFence, match="owner changed before native send"):
        await asyncio.wait_for(running, timeout=5)
    assert calls == [] and agent._inbox_cursors == {}
    assert await agent._drain_inbox("beta") == 0
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        row = store._connection.execute("SELECT session_id FROM native_runtime_inputs").fetchone()
        assert row is not None and row[0] is None


async def test_stable_existing_goal_allows_separate_selected_direct_reply(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(coordinated_runtime, "run_native_pi_turn", fake)
    original = comms.update_goal("beta", "set", text="Separate ongoing goal")
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    assert await agent._drain_inbox("beta") == 1
    assert len(calls) == 1
    assert comms.registry.require("beta").goal == original


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
