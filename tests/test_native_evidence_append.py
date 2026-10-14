"""Original private prefix verification permits append, not changed evidence."""

import asyncio
import io
import json
import os
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
        real_pread = os.pread
        passes = 0

        def reading(fd, size, offset):
            nonlocal passes
            if fd == source.stream.fileno() and offset == 0:
                passes += 1
            if passes == 2 and offset == 0 and phase == "hash_before":
                entered.set()
                assert finished.wait(5)
            data = real_pread(fd, size, offset)
            # The shepherd's case: mutate after bytes from the observed
            # revision were returned, including a multi-chunk prefix.
            if (passes == 1 and phase == "capture" or
                    passes > 1 and phase == "hash_after") and (
                    offset + len(data) == len(original) and not entered.is_set()
            ):
                entered.set()
                assert finished.wait(5)
            return data

        monkeypatch.setattr(os, "pread", reading)
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            changed_before_hash = phase == "hash_before" and change in {
                "prefix_and_append", "truncate",
            }
            if phase == "capture" or changed_before_hash:
                with pytest.raises(NativePiUnavailable):
                    tuple(source.rows())
                assert source.stream.closed
            else:
                assert len(tuple(source.rows())) == 2
                assert source.size == len(original)
                if change == "append":
                    assert tuple(source.rows())[0]["id"] == "concurrent-append"
                elif change == "rewrite":
                    assert tuple(source.rows()) == ()  # same bytes are unchanged evidence
                else:
                    # A mutation after this observation is fenced by the next
                    # observation, not by a racy final stat.
                    with pytest.raises(NativePiUnavailable):
                        tuple(source.rows())
        finally:
            entered.set()
            thread.join(5)
        assert not thread.is_alive() and not failures


def test_decode_uses_buffered_reader(tmp_path):
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as source:
        assert isinstance(source.stream, io.BufferedReader)
        assert len(tuple(source.rows())) == 2


def test_next_decode_cannot_reuse_prefetched_bytes(tmp_path):
    session = _evidence(tmp_path)
    first, old, new = b'{"id":"first"}\n', b'{"id":"old"}\n', b'{"id":"new"}\n'
    session.write_bytes(first + old)
    with PrivateEvidenceRead.open(session) as source:
        assert tuple(source._decode_captured(len(first)))[0]["id"] == "first"
        with session.open("r+b", buffering=0) as writer:
            os.pwrite(writer.fileno(), new, len(first))
        source.size = len(first)
        assert tuple(source._decode_captured(len(first + new)))[0]["id"] == "new"


def test_append_during_decoding_is_read_on_the_next_observation(tmp_path):
    session = _evidence(tmp_path)
    original = session.read_bytes()
    with PrivateEvidenceRead.open(session) as source:
        rows = source.rows()
        first = next(rows)
        append_to(session)
        assert len((first, *rows)) == 2
        assert source.size == len(original)
        assert tuple(source.rows())[0]["id"] == "concurrent-append"


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
