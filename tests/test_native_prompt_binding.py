"""Prelaunch expected-prompt binding: crash/UNKNOWN/owner-change ordering.

Fake native Pi only; real provider acceptance stays with the pinned executor.
"""

from __future__ import annotations
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from conftest import publish_initial
from binding_reads import binding_rows

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from native_proof_cases import (
    append_input_entry, emitted_context, read_proof_rows, recorded_context, write_proof_rows,
)

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.context_segments.selected import SelectedTriageSegment
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, StaleFence
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.historical_native_inputs import read_historical_native_inputs
from agent_comms.native_pi import (
    NativePiRpcLaunch,
    NativePiUnavailable,
    CommittedInput,
    read_tracked_input_commitment,
)
from agent_comms.native_prompt_binding import binding_store_path, install_prompt_binding_schema, native_request_digest
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession


@pytest.fixture
def tmp_path(tmp_path_factory):
    if os.name != "posix":
        pytest.skip("sealed runtime requires POSIX private directories")
    return tmp_path_factory.mktemp("native-binding")


def _root(tmp_path: Path):
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    people = [
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())),
        comms.registry.new_thread('alpha', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), task='math answers', model='openai-codex/gpt-6-sol'),
    ]
    for person in people:
        comms.registry.declare(person)
    root_id = comms.messaging.initialize_private_initial_protocol()
    message = publish_initial(comms, "sender", "#team", "Compute 17+25.")
    initial = comms.bus.log.read_delivery_cohort(root_id, message.seq)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        install_prompt_binding_schema(store)
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


def _is_triage_request(options: dict) -> bool:
    return any(
        issubclass(contribution.kind, SelectedTriageSegment)
        for contribution in options.get("context_contributions", ())
    )


def _fake_model(*, decision: str = "FULL", digest_override: str | None = None):
    calls: list[tuple[str, str]] = []

    async def fake(package, *, input_id, prompt, worktree, session, **_):
        session_dir, session_file = session.directory, session.path
        fresh = session_file is None
        if fresh:
            session_file = session_dir / f"{input_id}.jsonl"
            session_file.write_text(
                json.dumps({"type": "session", "version": 3, "timestamp": "2026-10-01T00:00:00.000Z", "id": f"isolated-session-{input_id}"}) + "\n"
            )
            session_file.chmod(0o600)
        assert session_file is not None

        def authorized():
            identity = NativeSessionIdentity(
                json.loads(session_file.read_text().splitlines()[0])["id"], str(session_file)
            )
            with _["prompt_send_boundary"](identity):
                calls.append((input_id, prompt))

        await asyncio.to_thread(authorized)
        entries = [json.loads(line) for line in session_file.read_text().splitlines()]
        proof_rows = [] if fresh else read_proof_rows(session_file)
        generation = 1 + max((row["requestGeneration"] for row in proof_rows), default=0)
        entry_id = hashlib.sha256(input_id.encode()).hexdigest()[:16]
        # Real pinned native semantics: the tracked digest covers the
        # pi-input-request-v1 request envelope, not bare prompt bytes.
        digest = digest_override or native_request_digest(prompt)
        session_entry_start = append_input_entry(session_file, {
            "type": "message", "timestamp": "2026-10-01T00:00:00.000Z",
            "id": entry_id,
            "message": {"role": "user", "inputId": input_id, "inputDigest": digest},
        })
        context_digest = hashlib.sha256((input_id + str(generation)).encode()).hexdigest()
        proof_rows.append(
            {
                "schema": 2,
                "type": "context_committed",
                "sessionId": entries[0]["id"],
                "inputId": input_id,
                "sessionEntryId": entry_id,
                "requestGeneration": generation,
                "llmContextDigest": context_digest,
                "committedAtMs": 1_791_964_800_000 + generation,
            }
        )
        write_proof_rows(session_file, proof_rows)
        reply = json.dumps({"decision": decision}) if _is_triage_request(_) else "42"
        from agent_comms.native_pi import NativeTurnResult

        observer = _.get("observe_event")
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


