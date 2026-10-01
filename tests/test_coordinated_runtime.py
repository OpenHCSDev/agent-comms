"""Receipt-backed runner with real bus/SQLite and fake Pi unit boundary.

These fakes test state transitions only. Parent owns real patched Pi/provider process
acceptance and final post-merge review; no fake can establish Pi model authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_comms import coordinated_runtime as runtime
from agent_comms import selected_turn
from agent_comms.historical_native_inputs import FullHistoricalNativeInput, TriageHistoricalNativeInput
from agent_comms.selected_triage import IgnoreSelectedTriage, FullSelectedTriage
from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.assignment_states import (
    CompletedAssignment,
    FailedAssignment,
    IgnoredAssignment,
    TriagePendingAssignment,
)
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.compaction_errors import CompactionJournalError, CompactionJournalUnknownError
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_private_inputs import PrivateInputs
from agent_comms.compaction_records import SelectedSummaryAttempt
from agent_comms.compaction_states import ReservedSummary
from agent_comms.coordinated_runtime import SelectedExecution
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
    _fresh_selected_revision,
)
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.optional_awareness_projection import OmittedAwareness, OptionalAwarenessProjection
from agent_comms.publisher import Publisher
from agent_comms.registration import Registration
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.wake_candidate_index import ProjectionUnavailableError, WakeCandidateIndex
from agent_comms.wake_injection import render_selected_wake_frame
from agent_comms.wake_policy import PassiveWake
from native_proof_cases import read_proof_rows, write_proof_rows
from selected_summary_cases import manual_source


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
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    people = [
        Thread(
            "sender",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        ),
        Thread(
            "alpha",
            frozenset({"team"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            task="release notes; ignore arithmetic tasks",
            model="openai-codex/gpt-6-sol",
        ),
        Thread(
            "beta",
            frozenset({"team"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            task="arithmetic answers",
            model="openai-codex/gpt-6-sol",
            thinking_level="high",
        ),
    ]
    for person in people:
        comms.registry.declare(person)
    root_id = comms.messaging.initialize_private_initial_protocol()
    target = "beta" if direct else "#team"
    body = body if body is not None else ("@beta Compute 17+25." if mentioned else "Compute 17+25.")
    message = comms.messaging.send_initial_cohort("sender", target, body)
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
    return root, root_id, comms, initial, people


def _fake_model(*, decision: str = "FULL", fail_on: int | None = None):
    calls = []

    async def fake(
        package,
        *,
        input_id,
        prompt,
        worktree,
        session_dir,
        session_file=None,
        **_kwargs,
    ):
        # The real Pi get_state returns a saved file BEFORE raw prompt send.
        fresh = session_file is None
        if fresh:
            session_file = session_dir / "one.jsonl"
            session_file.write_text(
                json.dumps({"type": "session", "id": "isolated-session"}) + "\n"
            )
            session_file.chmod(0o600)
        assert session_file is not None
        selected = _kwargs.get("fresh_selected")
        if selected is not None:
            assert selected.path == session_file
            selected.verify_prewrite()
            # Fake only Pi's exact two initial metadata appends. This is NOT
            # a CLI runtime receipt, provider call, or descendant proof.
            model_id = "f0f0f001"
            for entry in (
                {
                    "type": "model_change",
                    "id": model_id,
                    "parentId": selected.bootstrap_leaf_id,
                    "timestamp": "2026-09-26T00:00:00.000Z",
                    "provider": "openrouter",
                    "modelId": "z-ai/glm-5.3-flash",
                },
                {
                    "type": "thinking_level_change",
                    "id": "f0f0f002",
                    "parentId": model_id,
                    "timestamp": "2026-09-26T00:00:00.000Z",
                    "thinkingLevel": selected.selected_thinking_level,
                },
            ):
                with session_file.open("a") as stream:
                    stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
            revision = selected.verify_selected_startup()
        else:
            revision = None

        # Model only admission in its dedicated thread, not native receipt.
        def admitted():
            admission = _kwargs["prompt_send_boundary"]
            with (
                admission(session_file, revision)
                if selected is not None
                else admission(session_file)
            ):
                calls.append((input_id, prompt))

        await asyncio.to_thread(admitted)
        if fail_on == len(calls):
            raise NativePiUnavailable("fake backend process died")
        if fresh:
            entries = [{"type": "session", "id": "isolated-session"}]
            proof_rows = []
        else:
            entries = [json.loads(line) for line in session_file.read_text().splitlines()]
            proof_file = Path(str(session_file) + ".input-proof")
            proof_rows = read_proof_rows(session_file) if proof_file.exists() else []
        session_id = entries[0]["id"]
        generation = 1 + max((row["requestGeneration"] for row in proof_rows), default=0)
        entry_id = hashlib.sha256(input_id.encode()).hexdigest()[:16]
        entries.append(
            {
                "type": "message",
                "id": entry_id,
                "message": {
                    "role": "user",
                    "inputId": input_id,
                    "inputDigest": hashlib.sha256(
                        (
                            "pi-input-request-v1\n"
                            + json.dumps(
                                {
                                    "kind": "prompt",
                                    "text": prompt,
                                    "images": None,
                                    "streamingBehavior": None,
                                    "expandPromptTemplates": True,
                                    "source": "rpc",
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        ).encode()
                    ).hexdigest(),
                },
            }
        )
        if fresh:
            session_file.write_text("".join(json.dumps(row) + "\n" for row in entries))
        else:
            with session_file.open("a") as output:
                output.write(json.dumps(entries[-1]) + "\n")
        session_file.chmod(0o600)
        digest = hashlib.sha256((input_id + str(generation)).encode()).hexdigest()
        proof_rows.append(
            {
                "schema": 1,
                "type": "context_committed",
                "sessionId": session_id,
                "inputId": input_id,
                "sessionEntryId": entry_id,
                "requestGeneration": generation,
                "llmContextDigest": digest,
            }
        )
        proof_file = Path(str(session_file) + ".input-proof")
        write_proof_rows(session_file, proof_rows)
        reply = json.dumps({"decision": decision}) if "bounded triage" in prompt else "42"
        observer = _kwargs.get("observe_event")
        if observer is not None:
            from agent_comms.pi_events import PiEvent

            await observer(
                PiEvent.from_wire(
                    {
                        "type": "response",
                        "id": "native-prompt",
                        "command": "prompt",
                        "success": True,
                    }
                )
            )
            await observer(PiEvent.from_wire({"type": "context_committed", "inputId": input_id}))
        return NativeTurnResult(
            reply,
            NativeContextProof(input_id, session_id, entry_id, generation, digest, session_file),
        )

    return fake, calls


async def test_unmentioned_agent_channel_real_sqlite_two_distinct_mocked_decisions(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, initial, people = _root(tmp_path)
    assert len(initial.audience.recipients) == 2
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    first, alpha_calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", first)
    alpha = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    ).run()
    assert alpha is not None and alpha.disposition is IgnoredAssignment
    assert len(alpha_calls) == 1
    assert "── comms: 1 selected ──" in alpha_calls[0][1]
    assert f'"source_seq":{initial.message.seq}' in alpha_calls[0][1]
    assert "engage only if this concerns your assigned task" in alpha_calls[0][1]
    assert "No response obligation exists until triage engages" in alpha_calls[0][1]
    assert "you owe a response" not in alpha_calls[0][1]
    with Coordination(str(root / "coordination.sqlite3")) as store:
        ignored = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[1].created_at),
            source_seq=initial.message.seq,
        )
        assert len(ignored) == 1 and isinstance(ignored[0], TriageHistoricalNativeInput)
        assert ignored[0].decision is IgnoreSelectedTriage
        assert ignored[0].execution == TriageNativeExecution()
        # Fake journal contract checks the join only, not native acceptance.
        assert ignored[0].expected_prompt_equality_established
    assert len(comms.views.channel_history("#team")) == 1
    second, beta_calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", second)
    beta = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    ).run()
    assert beta is not None and beta.disposition is CompletedAssignment
    assert beta.exact_target == "#team" and beta.response_message_id
    assert len(beta_calls) == 2
    assert "engage only if this concerns your assigned task" in beta_calls[0][1]
    assert "expected: this is yours" in beta_calls[1][1]
    assert '"target":"#team"' in beta_calls[1][1]
    assert f'"source_seq":{initial.message.seq}' in beta_calls[1][1]
    assert "This frame is a read-only projection, not file-write permission" in beta_calls[1][1]
    response = comms.views.channel_history("#team")[-1]
    assert response.body == "42" and response.sender == "beta"
    assert "_agent_comms_private_v1" not in response.to_wire()
    assert (
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
        is None
    )
    with Coordination(str(root / "coordination.sqlite3")) as store:
        lookups = {person.name: stable_thread_lookup(person.created_at) for person in people}
        assert store.participants.get(lookups["alpha"]).pointer.execution_id is None
        assert store.participants.get(lookups["beta"]).pointer.execution_id is None
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM native_runtime_input WHERE session_id IS NOT NULL"
            ).fetchone()[0]
            == 3
        )
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM cohort_delivery_receipts"
            ).fetchone()[0]
            == 2
        )
    assert not (root / "read_markers.json").exists()


async def test_initial_no_wake_observer_never_enters_model_or_claim_page(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, initial, _ = _root(tmp_path, mentioned=True)
    assert len(initial.audience.recipients) == 2
    assert (
        sum(decision.__class__.__name__ == "NoWakeDecision" for decision in initial.decisions) == 1
    )
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", runner)
    assert (
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
        is None
    )
    assert not calls  # No-wake has no prompt frame or model invocation.
    assert len(comms.views.channel_history("#team")) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM cohort_delivery_receipts WHERE kind='unmentioned_observer'"
            ).fetchone()[0]
            == 1
        )


def test_wake_frame_rejects_no_wake_forgery_and_unengaged_full(tmp_path: Path) -> None:
    root, _root_id, comms, initial, people = _root(tmp_path, mentioned=True)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        alpha_lookup = stable_thread_lookup(people[1].created_at)
        beta_lookup = stable_thread_lookup(people[2].created_at)
        assert sealed_cohort_assignments(store, alpha_lookup) == ()
        selected = sealed_cohort_assignments(store, beta_lookup)[0]
    # A pending FULL claim has no response obligation, and no frame may grant one.
    with pytest.raises(IdentityConflict, match="response obligation"):
        render_selected_wake_frame(initial, selected, people[2])
    forged = replace(selected, recipient="alpha", recipient_lookup=alpha_lookup)
    with pytest.raises(IdentityConflict, match="selected N/K"):
        render_selected_wake_frame(initial, forged, people[1])
    assert len(comms.views.channel_history("#team")) == 1  # Framing never publishes a row.


def test_triage_frame_is_read_only_and_does_not_promote_message_body(tmp_path: Path) -> None:
    root, _root_id, comms, initial, people = _root(tmp_path)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        lookup = stable_thread_lookup(people[1].created_at)
        assignment = sealed_cohort_assignments(store, lookup)[0]
    frame = render_selected_wake_frame(initial, assignment, people[1])
    assert f'"source_seq":{initial.message.seq}' in frame
    assert '"wake_mode":"bounded_triage"' in frame
    assert initial.message.body not in frame
    assert "No response obligation exists until triage engages" in frame
    assert len(comms.views.channel_history("#team")) == 1


async def test_direct_selected_reply_goes_to_original_sender(tmp_path: Path, monkeypatch) -> None:
    root, root_id, comms, initial, people = _root(tmp_path, direct=True)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", runner)
    outcome = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    ).run()
    assert outcome is not None and outcome.exact_target == "sender"
    assert len(calls) == 1  # direct FULL, no separate triage invocation
    assert "expected: this is yours" in calls[0][1]
    assert '"audience":"direct"' in calls[0][1]
    assert '"target":"sender"' in calls[0][1]
    assert "you owe a response" in calls[0][1]
    with Coordination(str(root / "coordination.sqlite3")) as store:
        one = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[2].created_at),
            source_seq=initial.message.seq,
        )
        assert len(one) == 1 and isinstance(one[0].execution, FullNativeExecution)
        assert isinstance(one[0], FullHistoricalNativeInput)
        assert one[0].input_id == outcome.input_id
    assert comms.bus.dm_history("sender", "beta")[-1].target == "sender"


async def test_explicit_fresh_enrollment_precedes_fake_private_raw_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, root_id, _comms, _initial, people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    result = await SelectedExecution(
        root=root,
        wire_root_id=root_id,
        owner_name="beta",
        native_package=tmp_path,
        fresh_private_enrollment=True,
    ).run()
    assert result is not None and result.disposition is CompletedAssignment
    fresh = result.fresh_session
    assert fresh is not None and len(calls) == 1
    assert fresh.path.parent == root / "native-sessions" / stable_thread_lookup(
        people[2].created_at
    )
    fresh.verify_saved_identity()
    assert len(fresh.path.read_text().splitlines()) == 2
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    with journal.transaction() as db:
        coverage = db.execute(
            "SELECT session_id,device,inode,owner_generation,admission_generation "
            "FROM enrolled_private_sessions WHERE session_file=?",
            (str(fresh.path),),
        ).fetchone()
        assert coverage is not None and coverage[:3] == (
            fresh.session_id,
            fresh.file_identity.device,
            fresh.file_identity.inode,
        )
        assert db.execute(
            "SELECT input_id,status FROM private_raw_inputs WHERE session_file=?",
            (str(fresh.path),),
        ).fetchone() == (result.input_id, "unknown")
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.summaries.reserve(
            str(fresh.path),
            {
                "source": manual_source(fresh.path, "beta", incarnation=people[2].incarnation),
                "selected": {"provider": "openrouter", "modelId": "test", "contextWindow": 200000},
                "settings": {"keepRecentTokens": 2000, "reserveTokens": 1000},
            },
            fresh_session=fresh,
            admission_generation=coverage[4],
        )


async def test_explicit_selected_first_source_is_fenced_before_fake_raw_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, root_id, _comms, _initial, _people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    witnessed = []

    async def selected_runner(*args, **kwargs):
        selected = kwargs["fresh_selected"]
        assert selected is not None and selected.selected_thinking_level == "high"
        selected.verify_prewrite()  # Before any fake raw prompt reservation/write.
        witnessed.append(_fresh_selected_revision(selected))
        assert kwargs["session_file"] == selected.path
        return await runner(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", selected_runner)
    result = await SelectedExecution(
        root=root,
        wire_root_id=root_id,
        owner_name="beta",
        native_package=tmp_path,
        fresh_private_enrollment=True,
        selected_thinking_level="high",
    ).run()
    assert result is not None and result.disposition is CompletedAssignment
    assert len(calls) == 1 and len(witnessed) == 1
    assert witnessed[0].identity == result.fresh_session.file_identity
    assert result.fresh_session.selected_thinking_level == "high"
    result.fresh_session.verify_saved_identity()
    rows = [json.loads(row) for row in result.fresh_session.path.read_text().splitlines()]
    assert [row["type"] for row in rows[:3]] == ["session", "model_change", "thinking_level_change"]
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    with journal.transaction() as db:
        assert db.execute(
            "SELECT status FROM private_raw_inputs WHERE session_file=?",
            (str(result.fresh_session.path),),
        ).fetchone() == ("unknown",)


async def test_selected_startup_changed_after_state_denies_before_fake_raw_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, root_id, _comms, _initial, _people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    seen: list[Path] = []

    async def racing_runner(*args, **kwargs):
        admission = kwargs["prompt_send_boundary"]
        selected = kwargs["fresh_selected"]
        assert selected is not None
        seen.append(selected.path)

        def changed_before_admission(file, revision):
            assert file == selected.path and revision == selected.verify_selected_startup()
            with file.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "message",
                            "id": "dead0001",
                            "parentId": "f0f0f002",
                            "message": {"role": "user"},
                        }
                    )
                    + "\n"
                )
            return admission(file, revision)

        kwargs["prompt_send_boundary"] = changed_before_admission
        return await runner(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", racing_runner)
    with pytest.raises(NativePiUnavailable, match="startup"):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
            fresh_private_enrollment=True,
            selected_thinking_level="high",
        ).run()
    assert len(seen) == 1 and not calls
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    with journal.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM private_raw_inputs WHERE session_file=?",
                (str(seen[0]),),
            ).fetchone()[0]
            == 0
        )


async def test_fresh_creation_fsync_unknown_never_enters_fake_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_comms import fresh_private_session as fresh_module

    root, root_id, _comms, _initial, people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    expected_dir = root / "native-sessions" / stable_thread_lookup(people[2].created_at)
    real_fsync = fresh_module._fsync_directory

    def unknown_parent(path: Path) -> None:
        if path == expected_dir:
            raise OSError("injected fresh file parent fsync UNKNOWN")
        real_fsync(path)

    monkeypatch.setattr(fresh_module, "_fsync_directory", unknown_parent)
    with pytest.raises(NativePiUnavailable, match="durability UNKNOWN"):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
            fresh_private_enrollment=True,
        ).run()
    assert not calls
    visible = list(expected_dir.glob("enrolled-*.jsonl"))
    assert len(visible) == 1
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.summaries.reserve(
            str(visible[0]),
            {
                "source": manual_source(visible[0], "beta", incarnation=people[2].incarnation),
                "selected": {"provider": "openrouter", "modelId": "test", "contextWindow": 200000},
                "settings": {"keepRecentTokens": 2000, "reserveTokens": 1000},
            },
        )


@pytest.mark.parametrize("available", [True, False])
async def test_production_awareness_caller_includes_or_omits_without_losing_original(
    tmp_path: Path, monkeypatch, available: bool
) -> None:
    root, root_id, comms, initial, _people = _root(tmp_path, direct=True)
    if available:
        assert WakeCandidateIndex(comms.bus).maintain(rebuild=True)
    else:

        def unavailable(*_args, **_kwargs):
            raise ProjectionUnavailableError("candidate index unavailable")

        monkeypatch.setattr(WakeCandidateIndex, "page", unavailable)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    outcome = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
    ).run()
    assert outcome is not None and outcome.response_message_id
    assert len(calls) == 1
    assert initial.message.body in calls[0][1]
    assert ("Selected source decisions through " in calls[0][1]) is available
    if available:
        assert outcome.assignment_id in calls[0][1]
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_input WHERE assignment_id=?",
                (outcome.assignment_id,),
            ).fetchone()[0]
            == 1
        )


async def test_slow_optional_awareness_omits_without_blocking_selected_original(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    root, root_id, _comms, _initial, _people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    release = threading.Event()
    entered = threading.Event()

    def stalled_builder(_projection, _initial, _assignment, _owner):
        entered.set()
        release.wait(timeout=5)
        return OmittedAwareness("late context must not appear")

    monkeypatch.setattr(OptionalAwarenessProjection, "__call__", stalled_builder)
    try:
        outcome = await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
        ).run()
    finally:
        release.set()
    assert await asyncio.to_thread(OptionalAwarenessProjection._build_slot.acquire, True, 2)
    OptionalAwarenessProjection._build_slot.release()
    assert entered.is_set()
    assert outcome is not None and outcome.response_message_id
    assert len(calls) == 1 and "late context must not appear" not in calls[0][1]
    assert "Optional awareness omitted; original delivered alone" in caplog.text
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM native_runtime_input WHERE assignment_id=?",
                (outcome.assignment_id,),
            ).fetchone()[0]
            == 1
        )


async def test_repeated_awareness_timeouts_cannot_starve_unrelated_original(
    tmp_path: Path, monkeypatch
) -> None:
    first_base = tmp_path / "first"
    first_base.mkdir()
    first_root, _first_id, first_comms, first_initial, people = _root(first_base, direct=True)
    with Coordination(str(first_root / "coordination.sqlite3")) as store:
        assignment = sealed_cohort_assignments(store, stable_thread_lookup(people[2].created_at))[0]
    owner = first_comms.registry.require("beta")
    monkeypatch.setattr(OptionalAwarenessProjection, "build_seconds", 0.02)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked_builder(*_args):
        calls.append(True)
        entered.set()
        release.wait(timeout=3)
        return OmittedAwareness("too late")

    monkeypatch.setattr(OptionalAwarenessProjection, "__call__", blocked_builder)
    projection = OptionalAwarenessProjection.for_selected(
        WakeCandidateIndex(first_comms.bus),
        through_seq=first_initial.message.seq,
        generation=1,
        admission_generation=1,
    )
    try:
        assert await projection.render(first_initial, assignment, owner, 1024) == ""
        assert entered.is_set()
        for _ in range(40):
            assert await projection.render(first_initial, assignment, owner, 1024) == ""
        assert len(calls) == 1  # no queued/retired builder fleet
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=1) == 42
        second_base = tmp_path / "second"
        second_base.mkdir()
        second_root, second_id, _comms, _initial, _people = _root(second_base, direct=True)
        monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
        runner, native_calls = _fake_model()
        monkeypatch.setattr(TrackedTurnSession, "execute", runner)
        original = await asyncio.wait_for(
            SelectedExecution(
                root=second_root, wire_root_id=second_id, owner_name="beta", native_package=tmp_path
            ).run(),
            timeout=3,
        )
        assert original is not None and original.response_message_id
        assert len(native_calls) == 1
    finally:
        release.set()
        assert await asyncio.to_thread(OptionalAwarenessProjection._build_slot.acquire, True, 2)
        OptionalAwarenessProjection._build_slot.release()


@pytest.mark.parametrize("kind", ["complete", "incomplete", "oversize"])
async def test_optional_awareness_requires_complete_binding_and_prompt_budget(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    root, root_id, comms, initial, _people = _root(tmp_path, direct=True)
    assert WakeCandidateIndex(comms.bus).maintain(rebuild=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    if kind == "incomplete":

        def unavailable(*_args, **_kwargs):
            raise ProjectionUnavailableError("binding read incomplete")

        monkeypatch.setattr(WakeCandidateIndex, "page", unavailable)
    elif kind == "oversize":
        captured = OptionalAwarenessProjection.for_selected

        def small_budget(*args, **kwargs):
            return replace(captured(*args, **kwargs), max_text_bytes=1)

        monkeypatch.setattr(OptionalAwarenessProjection, "for_selected", small_budget)
    outcome = await SelectedExecution(
        root=root,
        wire_root_id=root_id,
        owner_name="beta",
        native_package=tmp_path,
    ).run()
    assert outcome is not None and outcome.response_message_id
    assert len(calls) == 1 and initial.message.body in calls[0][1]
    assert ("Selected source decisions through " in calls[0][1]) is (kind == "complete")
    assert ("Nonbinding rows omitted: 0" in calls[0][1]) is (kind == "complete")


async def test_selected_original_survives_auxiliary_cursor_over_100_initials(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, people = _root(tmp_path, direct=True)
    # These are committed frozen direct sources for another recipient. They
    # must not become beta's work or move beta's proven-injected cursor.
    for index in range(101):
        comms.messaging.send_initial_cohort("sender", "alpha", f"unrelated {index}")
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    outcome = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
    ).run()
    assert outcome is not None and outcome.response_message_id
    assert outcome.cursor_status == "proven"  # exact original only; not an unrelated ACK
    assert len(calls) == 1 and comms.views.dm_history("sender", "beta")[-1].body
    with Coordination(str(root / "coordination.sqlite3")) as store:
        cursor = NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="beta")
        assert cursor is not None and cursor.input_id == outcome.input_id
        response = next(
            message for message in comms.bus.log.full_history()
            if message.message_id == outcome.response_message_id
        )
        # The published own reply is nonbinding for beta, but belongs to the
        # certified scanned prefix. It cannot become a second injected source.
        assert cursor.covered_seq == response.seq == 103
        assert cursor.injected_seq == _initial.message.seq


async def test_historical_native_input_view_keeps_exact_triage_and_full_events(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, _comms, initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    engaged, calls = _fake_model(decision="FULL")
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", engaged)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[2].created_at),
                source_seq=initial.message.seq,
            )
            == ()
        )  # A selected pending claim has not accepted model input.
        with (
            store.session.transaction(),
            pytest.raises(IdentityConflict, match="committed snapshot"),
        ):
            read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[2].created_at),
                source_seq=initial.message.seq,
            )
    result = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    ).run()
    assert result is not None and result.disposition is CompletedAssignment
    assert len(calls) == 2
    with Coordination(str(root / "coordination.sqlite3")) as store:
        rows = read_historical_native_inputs(
            store,
            wire_root_id=root_id,
            recipient_lookup=stable_thread_lookup(people[2].created_at),
            source_seq=initial.message.seq,
        )
        assert [type(row.execution) for row in rows] == [TriageNativeExecution, FullNativeExecution]
        assert len({row.input_id for row in rows}) == 2
        assert rows[0].assignment_id == rows[1].assignment_id == result.assignment_id
        assert rows[0].execution == TriageNativeExecution()
        assert rows[0].decision is FullSelectedTriage
        assert rows[1].execution.require_attempt().execution_id and rows[1].execution.require_attempt().attempt_ordinal == 1
        assert isinstance(rows[1], FullHistoricalNativeInput)
        assert rows[0].owner_lookup == rows[1].owner_lookup
        assert rows[0].owner_generation == rows[1].owner_generation == 1
        assert all(row.expected_prompt_equality_established for row in rows)
        assert all(row.context.session_id == "isolated-session" for row in rows)
        assert (
            read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[1].created_at),
                source_seq=initial.message.seq,
            )
            == ()
        )  # Alpha's triage is pending, not a proof or max-seq gap to skip.
        journal = Path(str(rows[0].context.session_file) + ".input-proof")
        proofs = read_proof_rows(rows[0].context.session_file)
        proofs[0]["llmContextDigest"] = "0" * 64
        write_proof_rows(rows[0].context.session_file, proofs)
        with pytest.raises(IdentityConflict, match="differs from live-recorded proof"):
            read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[2].created_at),
                source_seq=initial.message.seq,
            )


def _reserved_private_selected_row(journal: CompactionJournal, session_file: Path) -> str:
    """An unresolved current reservation must block the raw writer."""
    operation_id = "a" * 32
    source = json.dumps(
        {
            "source": manual_source(session_file, "beta"),
            "selected": {"provider": "fake", "modelId": "test", "contextWindow": 200000},
            "settings": {"keepRecentTokens": 2000, "reserveTokens": 1000},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with journal.transaction() as db:
        SelectedSummaryAttempt(
            operation_id, str(session_file.resolve(strict=True)), source, ReservedSummary()
        ).insert(db)
    return operation_id


@pytest.mark.parametrize("status", ["reserved", "unknown", "declined-prestart"])
async def test_private_raw_send_refuses_same_session_selected_row_before_write(
    tmp_path: Path, monkeypatch, status: str
) -> None:
    root, root_id, comms, initial, people = _root(tmp_path, direct=True)
    lookup = stable_thread_lookup(people[2].created_at)
    session_dir = root / "native-sessions" / lookup
    session_dir.mkdir(parents=True, mode=0o700)
    session_file = session_dir / "saved.jsonl"
    original = json.dumps({"type": "session", "id": "isolated-session"}) + "\n"
    session_file.write_text(original)
    session_file.chmod(0o600)
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    operation_id = _reserved_private_selected_row(journal, session_file)
    if status == "unknown":
        journal.summaries.mark_unknown(operation_id)
    elif status == "declined-prestart":
        assert journal.summaries.decline_prestart(operation_id, "unsupported") is None
    assert journal.summaries.get(operation_id).state.declared_name == status
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model()
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", fake)
    with pytest.raises(CompactionJournalError, match="blocks native input"):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
            session_file=session_file,
            opt_in=True,
        ).run()
    assert calls == [] and session_file.read_text() == original
    assert not Path(str(session_file) + ".input-proof").exists()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        rows = store.session._connection.execute(
            "SELECT sent_owner_admission_generation, session_id FROM native_runtime_input"
        ).fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == (None, None)
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="beta")
            is None
        )
    assert (
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
            session_file=session_file,
            opt_in=True,
        ).run()
        is None
    )
    assert calls == []


async def test_private_raw_prewrite_fsync_unknown_never_dispatches_or_retries(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, initial, people = _root(tmp_path, direct=True)
    lookup = stable_thread_lookup(people[2].created_at)
    session_dir = root / "native-sessions" / lookup
    session_dir.mkdir(parents=True, mode=0o700)
    saved = session_dir / "saved.jsonl"
    saved.write_text(json.dumps({"type": "session", "id": "isolated-session"}) + "\n")
    saved.chmod(0o600)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    fake, calls = _fake_model()
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", fake)
    reserve = PrivateInputs.reserve

    def uncertain(self, session_file, input_id):
        fsync = os.fsync
        try:
            os.fsync = lambda _fd: (_ for _ in ()).throw(OSError("parent fsync denied"))
            reserve(self, session_file, input_id)
        finally:
            os.fsync = fsync

    monkeypatch.setattr(PrivateInputs, "reserve", uncertain)
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
            session_file=saved,
            opt_in=True,
        ).run()
    assert calls == []
    journal = CompactionJournal(root / "compaction-commits.sqlite3")
    with journal.transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM private_raw_inputs WHERE session_file = ?", (str(saved),)
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.summaries.reserve(
            str(saved),
            {
                "source": manual_source(saved, "beta", incarnation=people[2].incarnation),
                "selected": {"provider": "fake", "modelId": "test", "contextWindow": 200000},
                "settings": {"keepRecentTokens": 2000, "reserveTokens": 1000},
            },
        )
    assert (
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
            session_file=saved,
            opt_in=True,
        ).run()
        is None
    )
    assert calls == []


async def test_private_raw_send_rejects_renamed_saved_file_before_selected_bind(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, initial, people = _root(tmp_path, direct=True)
    lookup = stable_thread_lookup(people[2].created_at)
    session_dir = root / "native-sessions" / lookup
    session_dir.mkdir(parents=True, mode=0o700)
    saved = session_dir / "saved.jsonl"
    moved = session_dir / "moved.jsonl"
    saved.write_text(json.dumps({"type": "session", "id": "isolated-session"}) + "\n")
    saved.chmod(0o600)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    base, calls = _fake_model(fail_on=1)

    async def renamed(*args, **kwargs):
        saved.rename(moved)
        journal = CompactionJournal(root / "compaction-commits.sqlite3")
        _reserved_private_selected_row(journal, moved)
        return await base(*args, **kwargs)

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", renamed)
    with pytest.raises(IdentityConflict, match="exact saved session"):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
            session_file=saved,
            opt_in=True,
        ).run()
    assert calls == [] and not saved.exists() and moved.exists()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        row = store.session._connection.execute(
            "SELECT sent_owner_admission_generation,session_id FROM native_runtime_input"
        ).fetchone()
        assert row is not None and tuple(row) == (None, None)
        assert (
            NativeSourceCursor(comms.bus, store, wire_root_id=root_id).read(owner_name="beta")
            is None
        )


async def test_historical_native_input_view_omits_no_wake_and_reserved_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, _comms, initial, people = _root(tmp_path, mentioned=True)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[1].created_at),
                source_seq=initial.message.seq,
            )
            == ()
        )  # No-wake has no selected SQL claim and cannot gain a proof.
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    failing, calls = _fake_model(fail_on=1)
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", failing)
    with pytest.raises(NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM native_runtime_input WHERE session_id IS NULL"
            ).fetchone()[0]
            == 1
        )
        assert (
            read_historical_native_inputs(
                store,
                wire_root_id=root_id,
                recipient_lookup=stable_thread_lookup(people[2].created_at),
                source_seq=initial.message.seq,
            )
            == ()
        )


async def test_crash_after_triage_reservation_never_reissues_model(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model(fail_on=1)
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", runner)
    with pytest.raises(NativePiUnavailable):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
    assert (
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
        is None
    )
    assert len(calls) == 1
    history = comms.views.channel_history("#team")
    assert len(history) == 2 and history[-1].notice
    assert "input is uncertain" in history[-1].body
    with Coordination(str(root / "coordination.sqlite3")) as store:
        row = store.session._connection.execute(
            "SELECT c.disposition,i.session_id FROM wake_claims c "
            "JOIN native_runtime_input i ON i.assignment_id=c.assignment_id "
            "WHERE c.recipient='alpha'"
        ).fetchone()
        assert tuple(row) == ("deferred", None)


async def test_forged_dto_without_private_evidence_cannot_mark_context(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)

    async def forged(_package, *, input_id, session_dir, **_kw):
        return NativeTurnResult(
            '{"decision":"IGNORE"}',
            NativeContextProof(
                input_id, "fake", "fake", 1, "f" * 64, session_dir / "nonexistent.jsonl"
            ),
        )

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", forged)
    with pytest.raises(NativePiUnavailable):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
    rows = comms.views.channel_history("#team")
    assert len(rows) == 2 and rows[-1].notice
    assert "input is uncertain" in rows[-1].body


async def test_session_file_registration_during_native_triage_keeps_owner(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model(decision="IGNORE")

    async def register_session(*args, **kwargs):
        result = await runner(*args, **kwargs)
        current = comms.registry.require("alpha")
        comms.registry.declare(replace(current, session_file=str(root / "metadata.jsonl")))
        return result

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", register_session)
    outcome = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    ).run()
    assert outcome is not None and outcome.disposition is IgnoredAssignment
    assert len(calls) == 1


async def test_session_file_registration_during_native_full_turn_keeps_response(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()

    async def register_session(*args, **kwargs):
        result = await runner(*args, **kwargs)
        current = comms.registry.require("beta")
        comms.registry.declare(replace(current, session_file=str(root / "metadata.jsonl")))
        return result

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", register_session)
    outcome = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    ).run()
    assert outcome is not None and outcome.response_message_id
    assert len(calls) == 1
    assert len(comms.bus.dm_history("sender", "beta")) == 2


async def test_project_change_during_native_full_turn_denies_response(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _people = _root(tmp_path, direct=True)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()
    other_project = tmp_path / "other-project"
    other_project.mkdir()

    async def change_project(*args, **kwargs):
        result = await runner(*args, **kwargs)
        current = comms.registry.require("beta")
        before = comms.registry.snapshot().admission_generations["beta"]
        comms.registry.declare(replace(current, worktree=str(other_project)))
        assert comms.registry.snapshot().admission_generations["beta"] == before
        return result

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", change_project)
    with pytest.raises(StaleFence, match="owner stopped or changed"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert len(calls) == 1
    assert len(comms.bus.dm_history("sender", "beta")) == 1


async def test_owner_generation_revoked_during_native_triage_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, people = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, _ = _fake_model(decision="IGNORE")
    lookup = stable_thread_lookup(people[1].created_at)

    async def revoke(*args, **kwargs):
        result = await runner(*args, **kwargs)
        with Coordination(str(root / "coordination.sqlite3")) as rival:
            rival.participants.advance_generation(lookup, "alpha", expected_generation=1)
        return result

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", revoke)
    with pytest.raises(StaleFence):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
    assert len(comms.views.channel_history("#team")) == 1


@pytest.mark.parametrize(
    "malformed",
    ['{"decision":"IGNORE","decision":"FULL"}', '{"decision":[]}', "[]", "IGNORE"],
)
async def test_ambiguous_triage_is_not_a_synthetic_ignore_or_full(
    tmp_path: Path, monkeypatch, malformed: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model()

    async def bad(*args, **kwargs):
        result = await runner(*args, **kwargs)
        return replace(result, text=malformed)

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", bad)
    failed = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha",
        native_package=tmp_path, opt_in=True,
    ).run()
    assert failed.disposition is FailedAssignment
    assert len(calls) == 1
    assert "invalid triage decision" in comms.views.channel_history("#team")[-1].body
    notifications = comms.views.message_notifications((_initial.message,))[
        (_initial.message.seq, _initial.message.message_id)
    ]
    failure = next(item for item in notifications if item.recipient == "alpha")
    assert failure.state == "Failed" and not failure.busy
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM native_runtime_input WHERE session_id IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        claim = store.assignments.get(failed.assignment_id)
        assert type(claim.lifecycle) is FailedAssignment
        rejected = read_historical_native_inputs(
            store, wire_root_id=root_id, recipient_lookup=claim.recipient_lookup,
            source_seq=claim.wire_seq,
        )
        assert rejected[0].proves_triage_source(rejected)
        assert "decision" not in {item.name for item in fields(rejected[0])}
    assert await SelectedExecution(root=root, wire_root_id=root_id, owner_name="alpha",
                                   native_package=tmp_path).run() is None
    assert len(calls) == 1  # A rejected decision is never tried again.


async def test_registered_owner_stopped_during_model_cannot_settle(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, _ = _fake_model(decision="IGNORE")

    async def stop_owner(*args, **kwargs):
        result = await runner(*args, **kwargs)
        comms.registry.unregister("alpha")
        return result

    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", stop_owner)
    with pytest.raises(StaleFence, match="stopped or changed"):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="alpha",
            native_package=tmp_path,
            opt_in=True,
        ).run()
    assert len(comms.views.channel_history("#team")) == 1
    assert not comms.registry.status("alpha").active
    assert comms.registry.require("alpha").active_turn is None


async def test_stop_before_atomic_turn_lease_does_not_revive_or_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    original_claim = Registration.lease_live_turn_with_admission

    def stop_before_claim(self, *args, **kwargs):
        comms.registry.unregister("beta")
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(Registration, "lease_live_turn_with_admission", stop_before_claim)
    with pytest.raises(StaleFence, match="stopped or busy before native turn"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert not calls
    assert not comms.registry.status("beta").active
    assert comms.registry.require("beta").active_turn is None
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM native_runtime_input"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("mutation", ["stop_then_heartbeat", "other_owner_heartbeat"])
async def test_owner_generation_denies_revival_without_blocking_another_owner(
    tmp_path: Path, monkeypatch, mutation: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    original_claim = Registration.lease_live_turn_with_admission
    expected = comms.registry.require("beta")

    def change_registry_before_claim(self, *args, **kwargs):
        if mutation == "stop_then_heartbeat":
            comms.registry.unregister("beta")
            comms.registry.heartbeat("beta")
            assert comms.registry.require("beta") == expected
        else:
            comms.registry.heartbeat("alpha")
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(
        Registration, "lease_live_turn_with_admission", change_registry_before_claim
    )
    if mutation == "stop_then_heartbeat":
        with pytest.raises(StaleFence, match="stopped or busy before native turn"):
            await SelectedExecution(
                root=root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            ).run()
        assert not calls
        assert len(comms.bus.dm_history("sender", "beta")) == 1
    else:
        result = await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
        assert result is not None and result.disposition is CompletedAssignment
        assert len(calls) == 1
        assert len(comms.bus.dm_history("sender", "beta")) == 2
    assert comms.registry.require("beta").active_turn is None
    with Coordination(str(root / "coordination.sqlite3")) as store:
        inputs = store.session._connection.execute(
            "SELECT count(*) FROM native_runtime_input"
        ).fetchone()
        intents = store.session._connection.execute(
            "SELECT count(*) FROM publication_intents"
        ).fetchone()
        assert (inputs[0], intents[0]) == ((0, 0) if mutation == "stop_then_heartbeat" else (1, 1))


@pytest.mark.parametrize("rename_during_turn", [False, True])
async def test_alias_turn_cleanup_tracks_canonical_owner_even_after_rename(
    tmp_path: Path, monkeypatch, rename_during_turn: bool
) -> None:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.registry.declare(
        Thread(
            "sender",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    comms.registry.declare(
        Thread(
            "beta",
            frozenset({"team"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            model="openai-codex/gpt-6-sol",
        )
    )
    comms.registry.rename("beta", "gamma")
    root_id = comms.messaging.initialize_private_initial_protocol()
    incoming = comms.messaging.send_initial_cohort("sender", "gamma", "Compute 17+25")
    initial = comms.bus.log.read_delivery_cohort(root_id, incoming.seq)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        for recipient in initial.audience.recipients:
            store.participants.register(
                recipient.recipient_lookup,
                recipient.canonical_thread,
                recipient.canonical_thread,
                committed=True,
            )
        accept_delivery_cohort(comms.bus, root_id, incoming.seq, store)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model()

    async def maybe_rename(*args, **kwargs):
        result = await fake(*args, **kwargs)
        if rename_during_turn:
            comms.registry.rename("gamma", "delta")
        return result

    monkeypatch.setattr(TrackedTurnSession, "execute", maybe_rename)
    if rename_during_turn:
        with pytest.raises(StaleFence):
            await SelectedExecution(
                root=root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            ).run()
        assert comms.registry.require("delta").active_turn is None
        assert len(comms.bus.dm_history("sender", "gamma")) == 1
    else:
        result = await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
        assert result is not None and result.disposition is CompletedAssignment
        assert comms.registry.require("gamma").active_turn is None
        assert len(comms.bus.dm_history("sender", "gamma")) == 2
    assert len(calls) == 1


@pytest.mark.parametrize("stop_stage", ["before_intent", "after_intent"])
async def test_owner_stop_before_response_boundary_never_appends(
    tmp_path: Path, monkeypatch, stop_stage: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, _calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    method = (
        "prepare_fenced_response" if stop_stage == "before_intent" else "publish_fenced_response"
    )
    original = getattr(selected_turn, method)

    def stopped_before_boundary(*args, **kwargs):
        comms.registry.unregister("beta")  # Direct registry writer does NOT hold wire lock.
        return original(*args, **kwargs)

    monkeypatch.setattr(selected_turn, method, stopped_before_boundary)
    with pytest.raises(StaleFence, match="stopped or changed"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        receipts = store.session._connection.execute(
            "SELECT count(*) FROM publication_receipts"
        ).fetchone()
        dispatched = store.session._connection.execute(
            "SELECT count(*) FROM publication_append_dispatches"
        ).fetchone()
        assert receipts[0] == dispatched[0] == 0
        assert store.session._connection.execute("SELECT state FROM obligations").fetchone()[0] == (
            "pending" if stop_stage == "before_intent" else "publishing"
        )


async def test_registry_stop_during_response_append_linearizes_after_sql_commit(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, _calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    append = Publisher._publish_keyed_response_unlocked
    started, stopped = threading.Event(), threading.Event()
    workers: list[threading.Thread] = []

    def blocking_append(self, intent, *, conversation, registry_snapshot=None):
        def stop_owner():
            started.set()
            comms.registry.unregister("beta")
            stopped.set()

        worker = threading.Thread(target=stop_owner)
        workers.append(worker)
        worker.start()
        assert started.wait(2)
        assert not stopped.wait(0.1), "owner stop raced the locked response append"
        return append(self, intent, conversation=conversation, registry_snapshot=registry_snapshot)

    monkeypatch.setattr(Publisher, "_publish_keyed_response_unlocked", blocking_append)
    try:
        result = await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    finally:
        for worker in workers:
            worker.join(timeout=4)
    assert workers and all(not worker.is_alive() for worker in workers)
    assert result is not None and result.disposition is CompletedAssignment
    assert stopped.is_set() and not comms.registry.status("beta").active
    assert len(comms.bus.dm_history("sender", "beta")) == 2
    with Coordination(str(root / "coordination.sqlite3")) as store:
        state = store.session._connection.execute("SELECT state FROM obligations").fetchone()
        assert state[0] == "published"


@pytest.mark.parametrize("mutation", ["stop_then_heartbeat", "finish_turn"])
@pytest.mark.parametrize("boundary", ["before_tx1", "after_tx1"])
async def test_revoked_turn_never_prepares_or_appends_a_response(
    tmp_path: Path, monkeypatch, mutation: str, boundary: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    original = (
        selected_turn.prepare_fenced_response
        if boundary == "before_tx1"
        else selected_turn.publish_fenced_response
    )

    def revoke(*args, **kwargs):
        if mutation == "finish_turn":
            active = comms.registry.require("beta").active_turn
            assert active is not None
            comms.agents.finish_turn(comms.registry.require("beta").turn_lease)
        else:
            comms.registry.unregister("beta")
            comms.registry.heartbeat("beta")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        selected_turn,
        "prepare_fenced_response" if boundary == "before_tx1" else "publish_fenced_response",
        revoke,
    )
    with pytest.raises(StaleFence, match="turn"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert len(calls) == 1
    assert comms.registry.status("beta").active
    assert comms.registry.require("beta").active_turn is None
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert store.session._connection.execute("SELECT state FROM obligations").fetchone()[0] == (
            "pending" if boundary == "before_tx1" else "publishing"
        )
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM publication_append_dispatches"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM native_runtime_input WHERE session_id IS NOT NULL"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "boundary", ["before_run", "after_model", "before_tx1_forged", "after_tx1", "after_tx1_forged"]
)
async def test_saved_stopped_turn_cannot_regain_owner_authority(
    tmp_path: Path, monkeypatch, boundary: str
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    model, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", model)

    def revoke_and_restore() -> None:
        saved = comms.registry.require("beta")
        assert saved.active_turn is not None
        comms.registry.unregister("beta")
        comms.registry.register(saved)
        restored = comms.registry.require("beta")
        assert restored == replace(
            saved, active_turn=replace(saved.active_turn, admission_generation=None)
        )
        assert comms.registry.status("beta").active

    if boundary == "before_run":
        comms.agents.begin_turn("beta", "old-authorized-turn")
        revoke_and_restore()
        with pytest.raises(StaleFence, match="stopped or changed"):
            await SelectedExecution(
                root=root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            ).run()
        assert not calls
    elif boundary == "after_model":

        async def revoke_after_model(*args, **kwargs):
            result = await model(*args, **kwargs)
            revoke_and_restore()
            return result

        monkeypatch.setattr(TrackedTurnSession, "execute", revoke_after_model)
        with pytest.raises(StaleFence, match="stopped or changed"):
            await SelectedExecution(
                root=root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            ).run()
        assert len(calls) == 1
    else:
        method = (
            "prepare_fenced_response"
            if boundary == "before_tx1_forged"
            else "publish_fenced_response"
        )
        publish = getattr(selected_turn, method)

        def revoke_after_tx1(*args, **kwargs):
            revoked_generation = kwargs["owner_witness"].admission_generation
            revoke_and_restore()
            if boundary.endswith("forged"):
                # A same-UID caller can supply a fresh epoch in a dataclass.
                # The locked registry snapshot must attest that THIS turn was
                # created in that epoch, not merely compare caller assertions.
                new_generation = comms.registry.snapshot().admission_generations["beta"]
                assert new_generation != revoked_generation
                kwargs["owner_witness"] = replace(
                    kwargs["owner_witness"], admission_generation=new_generation
                )
            return publish(*args, **kwargs)

        monkeypatch.setattr(selected_turn, method, revoke_after_tx1)
        with pytest.raises(StaleFence, match="turn stopped or changed|turn witness is invalid"):
            await SelectedExecution(
                root=root,
                wire_root_id=root_id,
                owner_name="beta",
                native_package=tmp_path,
                opt_in=True,
            ).run()
        assert len(calls) == 1
    assert len(comms.bus.dm_history("sender", "beta")) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM publication_append_dispatches"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM publication_receipts"
            ).fetchone()[0]
            == 0
        )
        obligation = store.session._connection.execute("SELECT state FROM obligations").fetchone()
        assert (obligation[0] if obligation is not None else None) == (
            "publishing"
            if boundary.startswith("after_tx1")
            else ("pending" if boundary in {"after_model", "before_tx1_forged"} else None)
        )


async def test_existing_owner_turn_is_not_borrowed_or_consumed(tmp_path: Path, monkeypatch) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    comms.agents.begin_turn("beta", "existing-real-turn")
    original = comms.registry.require("beta").active_turn
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    runner, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", runner)
    with pytest.raises(StaleFence, match="stopped or busy before native turn"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert calls == []
    assert comms.registry.require("beta").active_turn == original
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM native_runtime_input"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.session._connection.execute(
                "SELECT count(*) FROM wake_claims WHERE disposition='engaged'"
            ).fetchone()[0]
            == 0
        )
    comms.agents.finish_turn(comms.registry.require("beta").turn_lease)
    result = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
    ).run()
    assert result is not None and result.disposition is CompletedAssignment
    assert len(calls) == 1


async def test_full_input_crash_leaves_no_publish_and_no_automatic_restart(
    tmp_path: Path, monkeypatch
) -> None:
    root, root_id, comms, _initial, _ = _root(tmp_path, direct=True)
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model(fail_on=1)
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", runner)
    with pytest.raises(NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
    assert (
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path, opt_in=True
        ).run()
        is None
    )
    assert len(calls) == 1
    rows = comms.bus.dm_history("sender", "beta")
    assert len(rows) == 2 and rows[-1].notice
    with Coordination(str(root / "coordination.sqlite3")) as store:
        row = store.session._connection.execute(
            "SELECT stage,session_id FROM native_runtime_input"
        ).fetchone()
        assert tuple(row) == ("full", None)
        assert (
            store.session._connection.execute("SELECT state FROM obligations").fetchone()[0]
            == "failed"
        )


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


async def test_settled_page_does_not_hide_later_selected_claim(tmp_path: Path, monkeypatch) -> None:
    """Page saturation is not an empty inbox; scaffolding is not model authority."""
    root, root_id, comms, _initial, people = _root(tmp_path)
    lookup = stable_thread_lookup(people[1].created_at)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        for number in range(100):
            message = comms.messaging.send_initial_cohort(
                "sender", "#team", f"Bounded selected-page item {number}"
            )
            accept_delivery_cohort(comms.bus, root_id, message.seq, store)
        first_page = sealed_cohort_assignments(store, lookup, limit=100)
        assert len(first_page) == 100
        for assignment in first_page:
            # Schema-legal terminal fixture only; no forged Pi context claim.
            store.session._connection.execute(
                (
                    "UPDATE wake_claims SET lifecycle=json_object('kind','ignored'),revision=revi"
                    "sion+1 WHERE assignment_id=? AND disposition='triage_pending'"
                ),
                (assignment.assignment_id,),
            )
        assert len(sealed_cohort_assignments(store, lookup, after_seq=first_page[-1].wire_seq)) == 1
    monkeypatch.setattr("agent_comms.coordinated_runtime._trusted_package", lambda _: None)
    runner, calls = _fake_model(decision="IGNORE")
    monkeypatch.setattr("agent_comms.tracked_turn.TrackedTurnSession.execute", runner)
    outcome = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="alpha", native_package=tmp_path, opt_in=True
    ).run()
    assert outcome is not None and outcome.disposition is IgnoredAssignment
    # The earlier selected rows lack native proof. Under xdist pressure the
    # best-effort 250 ms canonical scan may instead be unavailable; neither
    # status may advance a cursor or retry the current original.
    assert outcome.cursor_status in {"blocked_gap", "unavailable"}
    assert len(calls) == 1
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM current_native_cursor WHERE recipient_lookup=?",
                (lookup,),
            ).fetchone()[0]
            == 0
        )
    assert not (root / "read_markers.json").exists()


async def test_untrusted_pi_fails_before_any_bus_or_sql_mutation(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    with pytest.raises(NativePiUnavailable):
        await SelectedExecution(
            root=root, wire_root_id="0" * 32, owner_name="alpha", native_package=tmp_path
        ).run()
    assert not list(root.iterdir())


async def test_channel_triage_and_full_use_configured_owner_model(tmp_path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda package: package)
    fake, calls = _fake_model(decision="FULL")
    selections = []

    async def capture(package, **kwargs):
        selections.append((kwargs["provider"], kwargs["model"], kwargs["thinking_level"]))
        return await fake(package, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", capture)
    result = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
    ).run()
    assert result.disposition is CompletedAssignment
    assert selections == [("openai-codex", "gpt-6-sol", "high")] * 2
    assert len(calls) == 2


async def test_unconfigured_owner_does_not_reserve_or_launch(tmp_path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path)
    comms = Comms(root)
    owner = comms.registry.require("beta")
    comms.registry.register(replace(owner, model=None), comms.registry.status("beta"))
    monkeypatch.setattr(runtime, "_trusted_package", lambda package: package)
    fake, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    with pytest.raises(IdentityConflict, match="no configured provider/model"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
        ).run()
    assert calls == []
    assert comms.registry.require("beta").active_turn is None
    with Coordination(str(root / "coordination.sqlite3")) as store:
        pending = sealed_cohort_assignments(store, stable_thread_lookup(owner.created_at))
        assert type(pending[0].lifecycle) is TriagePendingAssignment


@pytest.mark.parametrize("direct", [True, False])
async def test_terminal_provider_failure_is_visible_nonwaking_and_frees_next_input(
    tmp_path, monkeypatch, direct
):
    from agent_comms.native_pi import NativePiTerminalFailure

    root, root_id, comms, initial, people = _root(tmp_path, direct=direct)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")

    async def failed(package, **kwargs):
        result = await fake(package, **kwargs)
        if "bounded triage" in kwargs["prompt"]:
            return result
        raise NativePiTerminalFailure(
            "Codex error: The usage limit has been reached",
            result.context,
            kwargs["provider"],
            kwargs["model"],
        )

    monkeypatch.setattr(TrackedTurnSession, "execute", failed)
    with pytest.raises(NativePiTerminalFailure, match="usage limit"):
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
        ).run()
    notice = comms.views.full_history()[-1]
    assert notice.notice and notice.type.value == "alert"
    assert notice.target == ("sender" if direct else "#team")
    assert "The usage limit has been reached" in notice.body
    assert "No automatic retry" in notice.body
    notice_initial = comms.bus.log.read_delivery_cohort(root_id, notice.seq)
    assert all(decision.wake_mode == PassiveWake() for decision in notice_initial.decisions)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        lookup = stable_thread_lookup(people[2].created_at)
        assert store.participants.get(lookup).pointer.execution_id is None
        assignment = sealed_cohort_assignments(store, lookup)[0]
        assert type(assignment.lifecycle) is FailedAssignment
    diagnostics = list((root / "diagnostics").glob("*.json"))
    assert len(diagnostics) == 1
    assert json.loads(diagnostics[0].read_text())["sequences"] == [initial.message.seq]
    before = len(calls)
    assert (
        await SelectedExecution(
            root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
        ).run()
        is None
    )
    assert len(calls) == before  # Failed input never replayed.
    fresh = comms.messaging.send_initial_cohort("sender", "beta", "New independent message")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        accept_delivery_cohort(comms.bus, root_id, fresh.seq, store)
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    result = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
    ).run()
    assert result.disposition is CompletedAssignment
    assert len(calls) == before + 1


async def test_current_work_context_reaches_both_triage_and_full(tmp_path, monkeypatch):
    from agent_comms.goals import Goal

    root, root_id, comms, _initial, _people = _root(tmp_path)
    owner = comms.registry.require("beta")
    comms.registry.register(
        replace(
            owner,
            task="Bootstrap: wait for a concrete task; do not modify files yet.",
            title="Channel delivery implementation",
            goal=Goal(
                text="Restore channel subscribers", id="current-goal", progress="Routing fixed"
            ),
        )
    )
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    result = await SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
    ).run()
    assert result.disposition is CompletedAssignment
    assert len(calls) == 2
    for _input_id, prompt in calls:
        context = json.loads(
            next(
                line.removeprefix("work_context: ")
                for line in prompt.splitlines()
                if line.startswith("work_context: ")
            )
        )
        assert context["title"] == "Channel delivery implementation"
        assert context["tags"] == ["team"]
        assert context["current_goal"]["text"] == "Restore channel subscribers"
        assert context["current_goal"]["progress"] == "Routing fixed"
        assert context["original_assignment"].startswith("Bootstrap:")
        assert "an old bootstrap instruction to wait for a task does not exclude" in prompt


async def test_selected_execution_cannot_be_run_twice_or_reentered(tmp_path, monkeypatch):
    root, root_id, comms, initial, people = _root(tmp_path, direct=True)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def paused(*args, **kwargs):
        entered.set()
        await finish.wait()
        return await fake(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", paused)
    execution = SelectedExecution(
        root=root, wire_root_id=root_id, owner_name="beta", native_package=tmp_path
    )
    task = asyncio.create_task(execution.run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(IdentityConflict, match="cannot be reused"):
            await execution.run()
        finish.set()
        result = await asyncio.wait_for(task, 10)
        assert result.response_message_id
        with pytest.raises(IdentityConflict, match="cannot be reused"):
            await execution.run()
        assert len(calls) == 1
        assert comms.registry.require("beta").active_turn is None
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_native_rpc_refusal_retains_private_details_without_replay(tmp_path, monkeypatch):
    from agent_comms.native_pi import NativePiPromptRejected
    from agent_comms.pi_commands import Prompt
    from agent_comms.pi_events import Response

    root, root_id, comms, initial, people = _root(tmp_path)
    monkeypatch.setattr(runtime, "_trusted_package", lambda _: None)
    failing, calls = _fake_model(fail_on=1)
    response = Response(
        command=Prompt,
        id="native-prompt",
        success=False,
        error="Native preflight refused: private failure detail",
    )

    async def refused(package, **kwargs):
        try:
            await failing(package, **kwargs)
        except NativePiUnavailable:
            raise NativePiPromptRejected(response) from None
        pytest.fail("Expected refused native attempt")

    monkeypatch.setattr(TrackedTurnSession, "execute", refused)
    with pytest.raises(NativePiPromptRejected):
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
        ).run()
    notice = comms.views.full_history()[-1]
    assert notice.notice and "input is uncertain" in notice.body
    assert "No automatic retry" in notice.body
    assert response.error not in notice.body
    (diagnostic,) = (root / "diagnostics").glob("*.json")
    evidence = json.loads(diagnostic.read_text())
    assert evidence["native_response"] == response.rejection_details()
    assert evidence["sequences"] == [initial.message.seq]
    assert len(calls) == 1
    assert (
        await SelectedExecution(
            root=root,
            wire_root_id=root_id,
            owner_name="beta",
            native_package=tmp_path,
        ).run()
        is None
    )
    assert len(calls) == 1
