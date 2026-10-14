"""Original private prefix verification permits append, not changed evidence."""

import asyncio
import json
import threading

import pytest

from agent_comms.native_pi import NativePiUnavailable, PrivateEvidenceRead
from test_native_pi import _evidence
from test_backend_native_lifecycle import native_backend

APPEND = (json.dumps({"type": "custom", "id": "concurrent-append",
                      "customType": "isolated-evidence-check", "data": {}}) + "\n").encode()


def append_to(path):
    with path.open("ab", buffering=0) as stream:
        stream.write(APPEND)


@pytest.mark.parametrize("phase", ["capture", "hash_before", "hash_after"])
@pytest.mark.parametrize("padding", [0, 300_000])
@pytest.mark.parametrize("change", ["append", "prefix_and_append", "truncate", "replace", "permissions", "rewrite"])
def test_source_change_during_original_read(tmp_path, monkeypatch, phase, padding, change):
    session = _evidence(tmp_path)
    session.write_bytes(session.read_bytes().replace(b"separate", b"x" * padding + b"separate"))
    original = session.read_bytes()
    entered, finished = threading.Event(), threading.Event()
    failures = []

    def writer():
        try:
            assert entered.wait(5)
            if change == "append":
                append_to(session)
            elif change == "prefix_and_append":
                session.write_bytes(original.replace(b"separate", b"changed!") + APPEND)
            elif change == "truncate":
                session.write_bytes(original[:10])
            elif change == "replace":
                replacement = session.with_suffix(".replacement")
                replacement.write_bytes(original + APPEND)
                replacement.chmod(0o600)
                replacement.replace(session)
            elif change == "permissions":
                session.chmod(0o644)
            else:
                session.write_bytes(original)
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    with PrivateEvidenceRead.open(session) as source:
        real_read = source.stream.read
        passes = 0

        def reading(size):
            nonlocal passes
            start = source.stream.tell()
            # FileIO.readline() reads individual bytes while decoding rows.
            # Count only PrivateEvidenceRead's bounded capture/hash chunks.
            if size > 1 and start == 0:
                passes += 1
            if passes == 2 and start == 0 and phase == "hash_before":
                entered.set()
                assert finished.wait(5)
            data = real_read(size)
            # The shepherd's case: mutate after bytes from the observed
            # revision were returned, including a multi-chunk prefix.
            if (passes == 1 and phase == "capture" or
                    passes > 1 and phase == "hash_after") and size > 1 and (
                    source.stream.tell() == len(original) and not entered.is_set()
            ):
                entered.set()
                assert finished.wait(5)
            return data

        monkeypatch.setattr(source.stream, "read", reading)
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            if change == "append" and phase != "capture":
                assert len(tuple(source.rows())) == 2
                assert source.size == len(original)
                assert tuple(source.rows())[0]["id"] == "concurrent-append"
            else:
                # Chunked capture cannot distinguish an append from a prefix
                # rewrite before unread chunks, so revision growth there fails
                # closed. Growth after capture is rehashed and admitted.
                with pytest.raises(NativePiUnavailable):
                    tuple(source.rows())
                assert source.stream.closed
        finally:
            entered.set()
            thread.join(5)
        assert not thread.is_alive() and not failures


def test_consecutive_appends_rehash_only_the_captured_prefix(tmp_path, monkeypatch):
    session = _evidence(tmp_path)
    original = session.read_bytes()
    with PrivateEvidenceRead.open(session) as source:
        real_read = source.stream.read
        reads = []
        passes = 0

        def reading(size):
            nonlocal passes
            start = source.stream.tell()
            data = real_read(size)
            if size > 1:
                if start == 0:
                    passes += 1
                reads.append((start, size))
                # Two consecutive hashing passes see growth after reading their
                # last byte. Neither can certify the old digest at the new revision.
                if passes in (2, 3) and source.stream.tell() == len(original):
                    append_to(session)
            return data

        monkeypatch.setattr(source.stream, "read", reading)
        assert len(tuple(source.rows())) == 2
        assert reads == [(0, len(original))] * 4  # capture + three bounded passes
        assert source.size == len(original)
        assert len(tuple(source.rows())) == 2  # later bytes decoded only now


def test_continuous_append_exhausts_owned_prefix_verification_resource(tmp_path, monkeypatch):
    session = _evidence(tmp_path)
    original = session.read_bytes()
    with PrivateEvidenceRead.open(session) as source:
        real_read = source.stream.read
        reads = 0
        passes = 0

        def reading(size):
            nonlocal reads, passes
            start = source.stream.tell()
            data = real_read(size)
            if size > 1:
                reads += 1
                if start == 0:
                    passes += 1
                if passes > 1 and source.stream.tell() == len(original):
                    append_to(session)
            return data

        monkeypatch.setattr(source.stream, "read", reading)
        with pytest.raises(NativePiUnavailable, match="did not become stable"):
            tuple(source.rows())
        assert reads == 1 + source.prefix_verification_attempts
        assert source.stream.closed
        assert session.read_bytes().startswith(original)


async def test_actual_saved_native_tool_start_uses_prefix_reader(native_backend, monkeypatch):
    """Actual SDK/child/tool broker/history; concurrent byte reads are checked above."""
    from agent_comms.messages import Message
    from agent_comms.tracked_turn import TrackedTurnSession
    from delivery_owner_fixture import reply_turn_finished

    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    resource = native.project / "read-fixture.txt"
    resource.write_text("Independent fixture read\n")
    native.provider.tool_call = ("read", {"path": str(resource)})
    native.provider.text = "Read completed after concurrent journal append."
    starts, verified = [], []
    tool_started = TrackedTurnSession.tool_started

    async def during_tool_start(turn, event):
        starts.append(event.tool_call_id)
        source = turn.evidence.source
        verify = source.verify_snapshot

        def verified_prefix(size, digest):
            verify(size, digest)
            verified.append((source.path, size))

        with monkeypatch.context() as observation:
            observation.setattr(source, "verify_snapshot", verified_prefix)
            await tool_started(turn, event)

    monkeypatch.setattr(TrackedTurnSession, "tool_started", during_tool_start)
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
        comms.messaging.send_message("sender", session, "Read the fixture and reply once.")
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 40)
        assert len(starts) == len(verified) == 1
        assert native.provider.posts == 2
        assert native.session.read_bytes().startswith(original)
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        results = [row["message"] for row in rows if row.get("type") == "message"
                   and row["message"].get("role") == "toolResult"]
        assert len(results) == 1 and not results[0].get("isError")
        assert any(native.provider.text in row.body for row in comms.views.full_history()
                   if isinstance(row, Message))
