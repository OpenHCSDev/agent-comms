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


@pytest.mark.parametrize("phase", ["capture", "hash"])
@pytest.mark.parametrize("change", ["append", "prefix_and_append", "truncate", "replace", "permissions", "rewrite"])
def test_source_change_during_original_read(tmp_path, monkeypatch, phase, change):
    session = _evidence(tmp_path)
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
        reads = 0

        def reading(size):
            nonlocal reads
            reads += 1
            # Complete capture first. During hash, alter the source before its
            # fresh read so bytes must be checked, not reused from a read buffer.
            trigger = reads == (1 if phase == "capture" else 2)
            if trigger and phase == "hash":
                entered.set()
                assert finished.wait(5)
            data = real_read(size)
            if trigger and phase == "capture":
                entered.set()
                assert finished.wait(5)
            return data

        monkeypatch.setattr(source.stream, "read", reading)
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            if change == "append":
                assert len(tuple(source.rows())) == 2
                assert source.size == len(original)
                assert tuple(source.rows())[0]["id"] == "concurrent-append"
            else:
                with pytest.raises(NativePiUnavailable):
                    tuple(source.rows())
                assert source.stream.closed
        finally:
            entered.set()
            thread.join(5)
        assert not thread.is_alive() and not failures


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
