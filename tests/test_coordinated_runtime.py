"""Selected framing and schema refusals against real bus and SQLite owners."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.context_segments.wake import SelectedWakeSegment
from agent_comms.coordinated_runtime import SelectedExecution, SessionSetupFailed
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationActivationBlocked,
)
from agent_comms.coordinator import Coordination
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.notification_progress import HumanReadProgress
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.thread_model import DeclaredModel
from agent_comms.thread_text import GivenThreadText
from agent_comms.turn_context import TurnContext
from bus_history import read_history
from bus_record_fixture import prepare_native_comms
from conftest import publish_initial


@pytest.fixture
def tmp_path(private_directory):
    """Use the shared private directory, including its joined bus retirement."""
    return private_directory


def _root(
    tmp_path: Path,
    *,
    direct: bool = False,
    mentioned: bool = False,
    claims: bool = False,
    body: str | None = None,
):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    (root / "runtime").mkdir(mode=0o700)
    comms = prepare_native_comms(Comms(root))
    people = [
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())),
        comms.registry.new_thread('alpha', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), task=GivenThreadText('release notes; ignore arithmetic tasks'), model=DeclaredModel('openai-codex/gpt-6-sol')),
        comms.registry.new_thread('beta', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), task=GivenThreadText('arithmetic answers'), model=DeclaredModel('openai-codex/gpt-6-sol'), thinking_level=HighThinkingLevel),
    ]
    for person in people:
        comms.registry.declare(person)
    root_id = comms.registry.prepare_native_root().namespace
    target = "beta" if direct else "#team"
    body = body if body is not None else ("@beta Compute 17+25." if mentioned else "Compute 17+25.")
    message = publish_initial(comms, "sender", target, body)
    initial = comms.bus.bus.delivery(message.seq)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        for recipient in initial.audience.recipients:
            store.participants.register(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        accepted = accept_delivery_cohort(comms.bus, root_id, message.seq, store)
        assert accepted.value.member_count == len(initial.audience.recipients)
    return root, root_id, comms, initial, people


def test_wake_frame_rejects_no_wake_forgery_and_unengaged_full(tmp_path: Path) -> None:
    root, _root_id, comms, initial, people = _root(tmp_path, mentioned=True)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        alpha_lookup = RecordedThreadIncarnation("fixture", people[1].created_at).lookup
        beta_lookup = RecordedThreadIncarnation("fixture", people[2].created_at).lookup
        assert sealed_cohort_assignments(store, alpha_lookup) == ()
        selected = sealed_cohort_assignments(store, beta_lookup)[0]
    # A pending FULL claim has no response obligation, and no frame may grant one.
    with pytest.raises(IdentityConflict):
        TurnContext.render_segments((SelectedWakeSegment.capture(((initial, selected),), people[2], comms.goals),)).text
    forged = replace(selected, recipient="alpha", recipient_lookup=alpha_lookup)
    with pytest.raises(IdentityConflict):
        TurnContext.render_segments((SelectedWakeSegment.capture(((initial, forged),), people[1], comms.goals),)).text
    assert len(read_history(comms, "#team")) == 1  # Framing never publishes a row.


def test_triage_frame_is_read_only_and_does_not_promote_message_body(tmp_path: Path) -> None:
    root, _root_id, comms, initial, people = _root(tmp_path)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        lookup = RecordedThreadIncarnation("fixture", people[1].created_at).lookup
        assignment = sealed_cohort_assignments(store, lookup)[0]
    frame = TurnContext.render_segments((SelectedWakeSegment.capture(((initial, assignment),), people[1], comms.goals),)).text
    assert f'"source_seq":{initial.message.seq}' in frame
    assert '"wake_mode":"bounded_triage"' in frame
    assert f'"body":{json.dumps(initial.message.body)}' in frame
    assert "No response obligation exists until triage engages" in frame
    assert len(read_history(comms, "#team")) == 1


def test_channel_wake_frame_carries_declared_goal_state(tmp_path: Path) -> None:
    from agent_comms.goals import Goal

    root, _root_id, comms, initial, people = _root(tmp_path)
    owner = comms.registry.require("alpha")
    comms.registry.register(replace(owner, goal=Goal(
        text="Restore subscribers", id="wake-goal", published_seq=0,
        progress="Routing fixed",
    )))
    with Coordination(str(root / "coordination.sqlite3")) as store:
        lookup = RecordedThreadIncarnation("fixture", people[1].created_at).lookup
        assignment = sealed_cohort_assignments(store, lookup)[0]
    frame = TurnContext.render_segments((SelectedWakeSegment.capture(
        ((initial, assignment),), comms.registry.require("alpha"), comms.goals,
    ),)).text
    context = json.loads(next(
        line.removeprefix("work_context: ") for line in frame.splitlines()
        if line.startswith("work_context: ")
    ))
    assert context["current_goal"] == {
        "text": "Restore subscribers", "status": {"kind": "active"},
        "progress": "Routing fixed",
    }
    assert context["original_assignment"] == "release notes; ignore arithmetic tasks"


def test_wake_frame_includes_bounded_changed_sent_outcomes(tmp_path: Path) -> None:
    from agent_comms.presentation import MessageNotification

    root, _root_id, _comms, initial, people = _root(tmp_path)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        lookup = RecordedThreadIncarnation("fixture", people[1].created_at).lookup
        assignment = sealed_cohort_assignments(store, lookup)[0]
    notification = MessageNotification(
        initial.audience.recipients[0], initial.audience.sender_lookup, HumanReadProgress(),
        message=initial.message, changed=True,
    )
    sent_outcome = notification.sent_outcome()
    assert sent_outcome is not None
    frame = TurnContext.render_segments((SelectedWakeSegment.capture(
        ((initial, assignment),), people[1], _comms.goals, sent_outcomes=(sent_outcome,)
    ),)).text
    assert 'recent_sent_outcomes: [{"source_seq":' in frame
    assert '"state":"Read"' in frame
    assert replace(notification, changed=False).sent_outcome() is None


def test_native_runtime_schema_explicit_install_and_drift_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    with Coordination(str(path)) as store:
        assert (
            store.session._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='native_runtime_input'"
            ).fetchone()
            is None
        )
        install_native_runtime_schema(store)
        install_native_runtime_schema(store)
        NativeRuntimeTable.require_installed(store.session._connection)
        trigger = next(iter(NativeRuntimeInput.triggers()))
        store.session._connection.execute(f'DROP TRIGGER "{trigger}"')
        with pytest.raises(PublicationActivationBlocked):
            NativeRuntimeTable.require_installed(store.session._connection)


async def test_untrusted_pi_fails_before_any_bus_or_sql_mutation(tmp_path: Path, *, native_runtime_package, cursor_delivery) -> None:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    root.chmod(0o755)  # Refuse an unsafe private root before opening its registry.
    with pytest.raises(SessionSetupFailed) as failure:
        await SelectedExecution(
            root=root,
            wire_root_id="0" * 32,
            owner_name="alpha",
            native_package=native_runtime_package,
            cursor_delivery=cursor_delivery,
        ).run()
    assert isinstance(failure.value.__cause__, NativePiUnavailable)
    assert not list(root.iterdir())


