"""Receipt-backed runner with real bus/SQLite and fake Pi unit boundary.

These fakes test state transitions only. Parent owns real patched Pi/provider process
acceptance and final post-merge review; no fake can establish Pi model authority.
"""

from __future__ import annotations

from bus_history import read_history
from agent_comms.channel_targets import CommandTarget
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from agent_comms.field_codec import FieldCodec
from agent_comms.diagnostics import RequestObservations
from conftest import publish_initial
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.messages import NoticeRole
import asyncio
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import fields, replace
from pathlib import Path

import pytest

import agent_comms.coordinated_runtime as runtime
import agent_comms.coordination_response as coordination_response
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.historical_native_inputs import FullHistoricalNativeInput, TriageHistoricalNativeInput
from agent_comms.selected_triage import IgnoreSelectedTriage, FullSelectedTriage
from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.assignment_states import (
    CompletedAssignment,
    FailedAssignment,
    IgnoredAssignment,
    TriagePendingAssignment,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_arguments import SelectedModel
from agent_comms.notification_progress import HumanReadProgress
from agent_comms.coordinated_runtime import SelectedExecution, SessionSetupFailed
from agent_comms.coordinated_runtime_schema import (
    assert_native_runtime_schema,
    install_native_runtime_schema,
)
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationActivationBlocked,
    StaleFence,
)
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_pi import (
    NativeContextProof,
    NativePiUnavailable,
    NativeTurnResult,
)
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.private_sidecar import native_request_digest
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.optional_awareness_projection import OptionalAwarenessProjection
from agent_comms.context_segments.optional_awareness import OmittedAwareness
from agent_comms.publisher import ResponseWrites
from agent_comms.registration import Registration
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.wake_candidate_index import (
    ProjectionUnavailableError, WakeCandidateIndex, RebuildIndex,
)
from agent_comms.context_segments.wake import SelectedWakeSegment
from agent_comms.turn_context import TurnContext
from agent_comms.wake_policy import PassiveWake
from native_proof_cases import (
    append_input_entry, emitted_context, read_proof_rows, recorded_context, write_proof_rows,
)


@pytest.fixture
def tmp_path():
    """The sealed runtime permits only owner-only disposable /var/tmp roots.

    CI's standard pytest temp root is /tmp; on hosts without a safe /var/tmp
    (including Windows and macOS with a symlinked /var), these Linux-only
    private-root process tests are inapplicable rather than weakening the runtime guard.
    """
    if (
        os.name != "posix"
        or not Path("/var/tmp").is_dir()
        or Path("/var").is_symlink()
        or Path("/var/tmp").is_symlink()
    ):
        pytest.skip("sealed runtime requires a real, disposable /var/tmp root")
    with tempfile.TemporaryDirectory(prefix="ac-sealed-test-", dir="/var/tmp") as root:
        yield Path(root)


def _root(
    tmp_path: Path,
    *,
    direct: bool = False,
    mentioned: bool = False,
    claims: bool = False,
    body: str | None = None,
):
    # The fake native process bypasses package verification, but normal FULL
    # launch still selects the packaged coding extension before process spawn.
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    extension = Path(__file__).parents[1] / "src/agent_comms/channel_coding_tools.mjs"
    (dist / extension.name).write_bytes(extension.read_bytes())
    import_fence = Path(__file__).parents[1] / "stack/native-import-fence.mjs"
    (dist / "agent-comms-import-fence.mjs").write_bytes(import_fence.read_bytes())
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    (root / "runtime").mkdir(mode=0o700)
    comms = Comms(root)
    people = [
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())),
        comms.registry.new_thread('alpha', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), task='release notes; ignore arithmetic tasks', model='openai-codex/gpt-6-sol'),
        comms.registry.new_thread('beta', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), task='arithmetic answers', model='openai-codex/gpt-6-sol', thinking_level=HighThinkingLevel),
    ]
    for person in people:
        comms.registry.declare(person)
    root_id = comms.messaging.initialize_private_initial_protocol()
    target = "beta" if direct else "#team"
    body = body if body is not None else ("@beta Compute 17+25." if mentioned else "Compute 17+25.")
    message = publish_initial(comms, "sender", target, body)
    initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
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
    # The global fixture deliberately disables background candidate workers.
    # Tests that consume selected awareness own their deterministic projection.
    assert WakeCandidateIndex(comms.bus.log).maintain(start=RebuildIndex())
    return root, root_id, comms, initial, people


def _fake_session_id(input_id: str) -> str:
    """The saved session id the fake model writes for a fresh input."""
    return f"isolated-session-{input_id}"


def _fake_model(*, decision: str = "FULL", fail_on: int | None = None):
    calls = []

    async def fake(
        package,
        *,
        input_id,
        prompt,
        worktree,
        session,
        **_kwargs,
    ):
        session_dir, session_file = session.directory, session.path
        # The real Pi get_state returns a saved file BEFORE raw prompt send.
        fresh = session_file is None
        if fresh:
            session_file = session_dir / f"{input_id}.jsonl"
            session_file.write_text(
                json.dumps({"type": "session", "version": 3, "id": _fake_session_id(input_id), "timestamp": "2026-09-26T00:00:00.000Z"}) + "\n"
            )
            session_file.chmod(0o600)
        assert session_file is not None
        # Model only claim_scope in its dedicated thread, not native acknowledgement.
        def authorized():
            claim_scope = _kwargs["prompt_send_boundary"]
            identity = NativeSessionIdentity(
                json.loads(session_file.read_text().splitlines()[0])["id"], str(session_file)
            )
            with claim_scope(identity):
                calls.append((input_id, prompt))

        await asyncio.to_thread(authorized)
        if fail_on == len(calls):
            raise NativePiUnavailable("fake backend process died")
        entries = [json.loads(line) for line in session_file.read_text().splitlines()]
        proof_file = Path(str(session_file) + ".input-proof")
        proof_rows = read_proof_rows(session_file) if proof_file.exists() else []
        session_id = entries[0]["id"]
        generation = 1 + max((row["requestGeneration"] for row in proof_rows), default=0)
        entry_id = hashlib.sha256(input_id.encode()).hexdigest()[:16]
        session_entry_start = append_input_entry(session_file, {
            "type": "message",
            "id": entry_id,
            "timestamp": "2026-09-26T00:00:00.000Z",
            "message": {
                "role": "user",
                "inputId": input_id,
                "inputDigest": native_request_digest(prompt),
            },
        })
        digest = hashlib.sha256((input_id + str(generation)).encode()).hexdigest()
        proof_rows.append(
            {
                "schema": 2,
                "type": "context_committed",
                "sessionId": session_id,
                "inputId": input_id,
                "sessionEntryId": entry_id,
                "requestGeneration": generation,
                "llmContextDigest": digest,
                "committedAtMs": 1_791_964_800_000 + generation,
            }
        )
        write_proof_rows(session_file, proof_rows)
        # The original selected stage owns this control's response grammar.
        # Relevance guidance can mention triage inside a FULL prompt too.
        from agent_comms.private_send_stage import TriageNativeSend

        claim_scope = _kwargs["prompt_send_boundary"]
        if isinstance(claim_scope.stage, TriageNativeSend):
            reply = json.dumps({"decision": decision})
        else:
            reply = "42"
        observer = _kwargs.get("observe_event")
        if observer is not None:
            from agent_comms.agent_events import PromptAccepted

            await observer(PromptAccepted())
            await observer(emitted_context(proof_rows[-1]))
        return NativeTurnResult(
            reply,
            recorded_context(
                session_file, proof_rows[-1], session_entry_start=session_entry_start,
            ),
        )

    return fake, calls


def test_wake_frame_rejects_no_wake_forgery_and_unengaged_full(tmp_path: Path) -> None:
    root, _root_id, comms, initial, people = _root(tmp_path, mentioned=True)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        alpha_lookup = RecordedThreadIncarnation("fixture", people[1].created_at).lookup
        beta_lookup = RecordedThreadIncarnation("fixture", people[2].created_at).lookup
        assert sealed_cohort_assignments(store, alpha_lookup) == ()
        selected = sealed_cohort_assignments(store, beta_lookup)[0]
    # A pending FULL claim has no response obligation, and no frame may grant one.
    with pytest.raises(IdentityConflict, match="response obligation"):
        TurnContext.render_segments((SelectedWakeSegment.capture(((initial, selected),), people[2], comms.goals),)).text
    forged = replace(selected, recipient="alpha", recipient_lookup=alpha_lookup)
    with pytest.raises(IdentityConflict, match="selected N/K"):
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
        assert_native_runtime_schema(store.session._connection)
        store.session._connection.execute("DROP TRIGGER native_runtime_input_delete_guard")
        with pytest.raises(PublicationActivationBlocked, match="drifted"):
            assert_native_runtime_schema(store.session._connection)


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


