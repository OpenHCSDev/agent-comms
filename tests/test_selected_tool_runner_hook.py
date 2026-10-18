"""Provider-free runner contract for the selected owner tool boundary."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import agent_comms.coordinated_runtime as runtime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.errors import RelationViolationError
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_input_record import FullNativeExecution
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_claim_epoch import UnrecordedNativeClaim
from agent_comms.selected_actions import SelectedExistingFileWrite
from agent_comms.selected_tool_broker import (
    SelectedToolIntent,
    SelectedToolRequest,
    SelectedWriteArguments,
)
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model, _root


@pytest.fixture
def nominal_broker_stub(monkeypatch):
    # Keep this stub focused on owner binding; broker and native tests cover IPC.
    import agent_comms.selected_tool_broker as broker

    intent_type = broker.SelectedToolIntent
    mode_type = broker.SelectedToolMode

    bound = []

    def selected_tool_mode_for_owner(comms, store, claim_scope, owner, session_dir, input_id):
        with store.session.read():
            row = NativeRuntimeInput.one(store.session._connection, input_id=input_id)
        assert row is not None
        assert claim_scope.wake_assignment_id in row.execution.source_assignment_ids(
            store.session._connection, input_id
        )
        assert row.owner_thread == owner
        assert row.owner_lookup == claim_scope.recipient_lookup
        assert row.attempt_ordinal == claim_scope.attempt_ordinal == 1
        assert row.stage is FullNativeExecution and row.sent_owner_claimed_generation == UnrecordedNativeClaim()
        assert (
            comms.registry.snapshot().claimed_generations[owner]
            == claim_scope.owner_claimed_generation
        )
        bound.append((claim_scope, owner, session_dir, input_id))
        return mode_type(lambda request: None)  # never invoke fake mutation

    monkeypatch.setattr(broker, "selected_tool_mode_for_owner", selected_tool_mode_for_owner)
    return intent_type, mode_type, bound


def test_owner_configuration_requires_nominal_intent_and_private_binding(
    private_directory, nominal_broker_stub
):
    intent_type, _mode_type, _bound = nominal_broker_stub
    wire = private_directory / "wire"
    wire.mkdir(mode=0o700)
    comms = Comms(wire)
    with pytest.raises(TypeError, match="nominal owner intent"):
        CommsAgent(comms, private_selected_tool_intent=object())
    with pytest.raises(PublicationActivationBlocked, match="private bus root"):
        CommsAgent(comms, private_selected_tool_intent=intent_type())
    comms.messaging.initialize_private_initial_protocol()
    agent = CommsAgent(comms, private_selected_tool_intent=intent_type())
    assert type(agent._private_selected_tool_intent) is intent_type


@pytest.mark.asyncio
async def test_operator_plan_and_tool_intent_are_exclusive(
    private_directory, monkeypatch, nominal_broker_stub
):
    intent_type, _mode_type, _bound = nominal_broker_stub
    root, root_id, _comms, _initial, _ = _root(private_directory, direct=True)
    path = private_directory / "existing.py"
    path.write_text("before")
    with pytest.raises(IdentityConflict, match="cannot share an operator file plan"):
        await runtime.SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=private_directory,
            selected_tool_intent=intent_type(),
            selected_existing_file_write=SelectedExistingFileWrite(
                ExistingFileClaim(Path(path)), b"after"
            ),
        ).run()
    assert path.read_text() == "before"


@pytest.mark.asyncio
async def test_default_full_has_normal_coding_tools_and_cooperative_claim_instruction(
    private_directory, monkeypatch
):
    root, root_id, _comms, _initial, _ = _root(private_directory, direct=True)
    fake, calls = _fake_model()
    kwargs_seen = []

    async def observed(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return await fake(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", observed)
    assert (
        await runtime.SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=private_directory
        ).run()
    ).publications
    assert len(calls) == 1
    assert "normal read, bash, edit and write tools." in calls[0][1]
    assert "selected_claimed_write" not in calls[0][1]
    from agent_comms.channel_coding_tools import CodingToolMode

    assert isinstance(kwargs_seen[0]["selected_tool_mode"], CodingToolMode)


@pytest.mark.asyncio
async def test_real_owner_selected_tool_writes_existing_file_once(private_directory, monkeypatch):
    root, root_id, comms, _initial, _people = _root(private_directory, direct=True, claims=True)
    path = private_directory / "notes.txt"
    path.write_text("before", encoding="utf-8")
    fake, calls = _fake_model()

    async def selected_model(*args, **kwargs):
        result = await fake(*args, **kwargs)
        mode = kwargs["selected_tool_mode"]
        mode.action(SelectedToolRequest("call_1", SelectedWriteArguments("notes.txt", "after")))
        return result

    monkeypatch.setattr(TrackedTurnSession, "execute", selected_model)
    result = await runtime.SelectedExecution(
        root=root,
        wire_root_id=root_id,
        owner_name="beta",
        native_package=private_directory,
        selected_tool_intent=SelectedToolIntent(),
    ).run()
    assert result is not None and result.publications
    assert len(calls) == 1
    assert path.read_text(encoding="utf-8") == "after"
    assert (root / "native-sessions").is_dir()
    reply, = comms.bus.log.messages_for_references(
        tuple(publication.reference for publication in result.publications)
    )
    assert reply.body == "42"


@pytest.mark.asyncio
async def test_nominal_full_binds_exact_reserved_owner_input_and_gated_prompt(
    private_directory, monkeypatch, nominal_broker_stub
):
    intent_type, mode_type, bound = nominal_broker_stub
    root, root_id, comms, initial, people = _root(private_directory, direct=True)
    fake, calls = _fake_model()
    kwargs_seen = []

    async def observed(*args, **kwargs):
        kwargs_seen.append(kwargs)
        mode = kwargs.get("selected_tool_mode")
        assert type(mode) is mode_type and callable(mode.action)
        result = await fake(*args, **kwargs)
        with Coordination(str(root / "coordination.sqlite3")) as store:
            row = NativeRuntimeInput.one(store.session._connection, input_id=kwargs["input_id"])
        assert row.sent_owner_claimed_generation.matches(bound[0][0].owner_claimed_generation)
        return result

    monkeypatch.setattr(TrackedTurnSession, "execute", observed)
    result = await runtime.SelectedExecution(
        root=root,
        wire_root_id=root_id,
        owner_name="beta",
        native_package=private_directory,
        selected_tool_intent=intent_type(),
    ).run()
    assert result is not None and result.publications
    assert len(calls) == len(kwargs_seen) == len(bound) == 1
    claim_scope, owner, session_dir, input_id = bound[0]
    assert owner == "beta" and claim_scope.source_seq == initial.message.seq
    assert claim_scope.source_message_id == initial.message.message_id
    assert claim_scope.recipient_lookup == RecordedThreadIncarnation("fixture", people[2].created_at).lookup
    assert claim_scope.wire_root_id == root_id and claim_scope.attempt_ordinal == 1
    assert input_id == kwargs_seen[0]["input_id"]
    assert session_dir == kwargs_seen[0]["session"].directory
    assert "selected_claimed_write at most once" in calls[0][1]
    assert "using no tools" not in calls[0][1]
    assert not any(message.claim_transition for message in list(comms.views.full_history_page().messages))


@pytest.mark.asyncio
async def test_wrong_intent_or_observer_cannot_create_tool_mode(
    private_directory, monkeypatch, nominal_broker_stub
):
    intent_type, _mode_type, bound = nominal_broker_stub
    root, root_id, _comms, _initial, _ = _root(private_directory, mentioned=True)
    with pytest.raises(TypeError, match="nominal owner intent"):
        await runtime.SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=private_directory,
            selected_tool_intent=object(),
        ).run()
    assert (
        await runtime.SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=private_directory,
            selected_tool_intent=intent_type(),
        ).run()
        is None
    )
    assert not bound


@pytest.mark.asyncio
async def test_opted_in_triage_remains_no_tools(private_directory, monkeypatch, nominal_broker_stub):
    intent_type, _mode_type, bound = nominal_broker_stub
    root, root_id, _comms, _initial, _ = _root(private_directory)
    fake, calls = _fake_model(decision="IGNORE")
    kwargs_seen = []

    async def observed(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return await fake(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", observed)
    await runtime.SelectedExecution(
        root=root,
        wire_root_id=root_id,
        owner_name="alpha",
        native_package=private_directory,
        selected_tool_intent=intent_type(),
    ).run()
    assert len(calls) == len(kwargs_seen) == 1
    assert kwargs_seen[0]["selected_tool_mode"].launch_arguments(private_directory) == ("--no-tools",)
    assert "selected_claimed_write" not in calls[0][1]
    assert not bound


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "wrong_input", "stale_epoch"])
async def test_selected_full_failure_never_reissues_or_forges_response(
    private_directory, monkeypatch, nominal_broker_stub, failure
):
    intent_type, _mode_type, bound = nominal_broker_stub
    root, root_id, comms, _initial, _people = _root(private_directory, direct=True)
    fake, calls = _fake_model(fail_on=1 if failure == "unknown" else None)

    async def failed(*args, **kwargs):
        result = await fake(*args, **kwargs)
        if failure == "wrong_input":
            original = result.context.input_id
            different = ("1" if original[0] == "0" else "0") + original[1:]
            return replace(result, context=replace(result.context, input_id=different))
        if failure == "stale_epoch":
            # Disposable-only simulated claim_scope revocation, not a live
            # stop/restart or destructive registry state transition.
            with comms.registry.store.editing() as edit:
                edit.document.claims.advance("beta")
                edit.commit()
        return result

    monkeypatch.setattr(TrackedTurnSession, "execute", failed)
    with pytest.raises((NativePiUnavailable, StaleFence, IdentityConflict, RelationViolationError)):
        await runtime.SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=private_directory,
            selected_tool_intent=intent_type(),
        ).run()
    assert (
        await runtime.SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=private_directory,
            selected_tool_intent=intent_type(),
        ).run()
        is None
    )
    assert len(calls) == len(bound) == 1
    assert not any(message.claim_transition for message in list(comms.views.full_history_page().messages))
