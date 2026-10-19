"""Original private prefix verification permits append, not changed evidence."""

import asyncio
import hashlib
import io
import json
import os

import pytest

from agent_comms.native_entries import NativeEntry
from agent_comms.native_pi import (
    NativePiUnavailable,
    PrivateEvidenceRead,
    VerifiedNativeEvidencePrefix,
)
from agent_comms.private_path import FileRevision
from test_native_pi import _evidence

APPEND = (json.dumps({"type": "custom", "id": "concurrent-append",
                      "customType": "isolated-evidence-check", "data": {}}) + "\n").encode()


def append_to(path):
    with path.open("ab", buffering=0) as stream:
        stream.write(APPEND)


def test_unchanged_verified_revision_reads_no_bytes(tmp_path, monkeypatch):
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as source:
        assert len(tuple(source.rows())) == 2

        def unexpected_read(*_args):
            raise AssertionError("unchanged verified prefix must not be read again")

        monkeypatch.setattr(os, "pread", unexpected_read)
        assert tuple(source.rows()) == ()


def test_append_hashes_only_new_bytes(tmp_path, monkeypatch):
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as source:
        assert len(tuple(source.rows())) == 2
        append_to(session)
        real_pread = os.pread
        read = []

        def counted(fd, size, offset):
            raw = real_pread(fd, size, offset)
            read.append((offset, len(raw)))
            return raw

        monkeypatch.setattr(os, "pread", counted)
        assert tuple(source.rows())[0]["id"] == "concurrent-append"
        assert read == [(source.size - len(APPEND), len(APPEND))]


def test_non_growth_revision_change_reverifies_prefix(tmp_path):
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as source:
        assert len(tuple(source.rows())) == 2
        session.write_bytes(session.read_bytes().replace(b"separate", b"changed!"))
        with pytest.raises(NativePiUnavailable, match="prefix changed"):
            tuple(source.rows())
        assert source.stream.closed


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
        source.prefix = VerifiedNativeEvidencePrefix(
            len(first),
            hashlib.sha256(first),
            FileRevision.from_stat(os.fstat(source.stream.fileno())),
        )
        assert tuple(source._decode_captured(len(first + new)))[0]["id"] == "new"


def test_growth_during_prefix_read_keeps_the_captured_entries(tmp_path, monkeypatch):
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as source:
        read_chunks = source._pread_chunks
        appended = False

        def grow_after_captured_chunk(start, stop):
            nonlocal appended
            for raw in read_chunks(start, stop):
                yield raw
                if not appended:
                    append_to(session)
                    appended = True

        monkeypatch.setattr(source, "_pread_chunks", grow_after_captured_chunk)
        assert len(tuple(source.rows())) == 2
        assert tuple(source.rows())[0]["id"] == "concurrent-append"


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
        comms.messaging.send("sender", session, "Read the fixture and reply once.").message
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 40)
        assert len(starts) == len(verified) == 1
        assert native.provider.posts == 2
        assert native.session.read_bytes().startswith(original)
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        results = [row["message"] for row in rows if row.get("type") == "message"
                   and row["message"].get("role") == "toolResult"]
        assert len(results) == 1 and not results[0].get("isError")
        assert any(native.provider.text in row.body for row in list(comms.views.full_history_page().messages)
                   if isinstance(row, Message))
