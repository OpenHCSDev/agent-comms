"""Original private prefix verification permits append, not changed evidence."""

import io
import json
import os

import pytest

from agent_comms.native_pi import PrivateEvidenceRead, VerifiedNativeEvidencePrefix
from agent_comms.native_failures import NativePiUnavailable
from agent_comms.private_path import FileRevision
from test_native_pi import _evidence

APPEND = (json.dumps({"type": "custom", "timestamp": "2026-10-01T00:00:00.000Z", "id": "concurrent-append",
                      "customType": "isolated-evidence-check", "data": {}}) + "\n").encode()


@pytest.fixture
def session(tmp_path):
    """The evidence fixture's private session transcript."""
    return _evidence(tmp_path).path


def append_to(path):
    with path.open("ab", buffering=0) as stream:
        stream.write(APPEND)


def test_unchanged_verified_revision_reads_no_bytes(session, monkeypatch):
    with PrivateEvidenceRead.open(session) as source:
        assert len(tuple(source.rows())) == 2

        def unexpected_read(*_args):
            raise AssertionError("unchanged verified prefix must not be read again")

        monkeypatch.setattr(os, "pread", unexpected_read)
        assert tuple(source.rows()) == ()


def test_append_checks_only_new_tail_byte(session, monkeypatch):
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
        assert read == [(source.size - 1, 1)]


def test_non_growth_revision_change_reverifies_prefix(session):
    with PrivateEvidenceRead.open(session) as source:
        assert len(tuple(source.rows())) == 2
        session.write_bytes(session.read_bytes().replace(b"separate", b"changed!"))
        with pytest.raises(NativePiUnavailable):
            tuple(source.rows())
        assert source.stream.closed


def test_decode_uses_buffered_reader(session):
    with PrivateEvidenceRead.open(session) as source:
        assert isinstance(source.stream, io.BufferedReader)
        assert len(tuple(source.rows())) == 2


def test_next_decode_cannot_reuse_prefetched_bytes(session):
    first, old, new = b'{"id":"first"}\n', b'{"id":"old"}\n', b'{"id":"new"}\n'
    session.write_bytes(first + old)
    with PrivateEvidenceRead.open(session) as source:
        assert tuple(source._decode_captured(len(first)))[0]["id"] == "first"
        with session.open("r+b", buffering=0) as writer:
            os.pwrite(writer.fileno(), new, len(first))
        source.prefix = VerifiedNativeEvidencePrefix(
            len(first),
            FileRevision.from_stat(os.fstat(source.stream.fileno())),
        )
        # This fixture consumed record one; verification and decode positions
        # are separate now that an identity read can verify without decoding.
        source.decoded_through = len(first)
        assert tuple(source._decode_captured(len(first + new)))[0]["id"] == "new"


def test_growth_during_tail_check_keeps_the_captured_entries(session, monkeypatch):
    with PrivateEvidenceRead.open(session) as source:
        pread = os.pread
        appended = False

        def grow_after_captured_tail(fd, size, offset):
            nonlocal appended
            raw = pread(fd, size, offset)
            if not appended:
                append_to(session)
                appended = True
            return raw

        monkeypatch.setattr(os, "pread", grow_after_captured_tail)
        assert len(tuple(source.rows())) == 2
        assert tuple(source.rows())[0]["id"] == "concurrent-append"


def test_append_during_decoding_is_read_on_the_next_observation(session):
    original = session.read_bytes()
    with PrivateEvidenceRead.open(session) as source:
        rows = source.rows()
        first = next(rows)
        append_to(session)
        assert len((first, *rows)) == 2
        assert source.size == len(original)
        assert tuple(source.rows())[0]["id"] == "concurrent-append"


