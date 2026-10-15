"""Native parent links, not physical proximity, own an input's provenance."""

import json
import os

import pytest

from agent_comms.native_entries import NativeEntry
from agent_comms.native_transcript import NativeTranscript


def native_rows(input_id="a" * 32, session_id="original"):
    def message(identity, parent, role, *, tracked=None):
        body = {"role": role, "content": [{"type": "text", "text": identity}]}
        if tracked is not None:
            body.update(inputId=tracked, inputDigest="d" * 64)
        return {"type": "message", "id": identity, "parentId": parent, "message": body}

    return (
        {"type": "session", "id": session_id, "version": 3},
        message("ua", None, "user", tracked=input_id),
        message("step", "ua", "assistant"),
        {"type": "extension", "id": "opaque", "parentId": "step", "future": True},
        message("ub", "ua", "user", tracked="b" * 32),
        message("rb", "ub", "assistant"),
        message("uc", "ua", "user"),
        {"type": "extension", "id": "other-opaque", "parentId": "uc"},
        message("rc", "other-opaque", "assistant"),
        message("ra", "opaque", "assistant"),
        message("root", None, "assistant"),
        message("missing", "absent-parent", "assistant"),
    )


def write_native(path, rows):
    path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))


@pytest.mark.parametrize("targets", [("ra",), ("rb", "ra"), ("rc", "ra", "root", "missing")])
def test_original_branch_and_untracked_boundary_across_gaps(tmp_path, monkeypatch, targets):
    path = tmp_path / "session.jsonl"
    write_native(path, native_rows())
    with NativeTranscript(path).reading() as reader:
        originals = {record.entry.source_coordinates.id: record
                     for record in reader.forward(0, reader.cut().offset)}

        def no_ancestor_body_decode(raw):
            pytest.fail("A parent lookup re-decoded an original message body")

        monkeypatch.setattr(NativeEntry, "read", no_ancestor_body_decode)
        found = reader.inputs_for(tuple(originals[name] for name in targets))
        expected = {"ra": "ua", "rb": "ub", "rc": "uc", "root": None, "missing": None}
        assert tuple(entry.id if entry is not None else None for entry in found) == tuple(
            expected[name] for name in targets
        )
        for name, entry in zip(targets, found, strict=True):
            if name == "ra":
                assert entry.input_id == "a" * 32
            if name == "rc":
                assert entry.input_id is None  # Never borrow ua's earlier tracked proof.


def test_complete_fragment_reuses_its_original_entries(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    write_native(path, native_rows())
    with NativeTranscript(path).reading() as reader:
        originals = tuple(reader.forward(0, reader.cut().offset))

        def no_coordinate_decode(*args):
            pytest.fail("The complete original fragment was decoded again")

        monkeypatch.setattr(NativeEntry, "read_ancestry", no_coordinate_decode)
        found = reader.inputs_for(originals)
        assert tuple(entry.id if entry is not None else None for entry in found) == (
            None, "ua", "ua", "ua", "ub", "ub", "uc", "uc", "uc", "ua", None, None
        )


def test_reading_retains_its_original_inode_header_and_parent_chain(tmp_path):
    path = tmp_path / "session.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    write_native(path, native_rows())
    with NativeTranscript(path).reading() as reader:
        cut = reader.cut()
        originals = tuple(reader.forward(0, cut.offset))
        write_native(replacement, native_rows("e" * 32, "replacement"))
        os.replace(replacement, path)
        assert reader.session_id == "original"
        assert reader.cut().revision.identity == cut.revision.identity
        assert reader.input_for(originals[9]).input_id == "a" * 32
        assert NativeTranscript(path).session_id == "replacement"
    with pytest.raises(ValueError, match="closed file"):
        _ = reader.session_id


@pytest.mark.parametrize("ascending", [True, False])
def test_nested_ancestry_and_header_reads_do_not_move_traversal_cursor(tmp_path, ascending):
    path = tmp_path / "session.jsonl"
    rows = native_rows()
    write_native(path, rows)
    with NativeTranscript(path).reading() as reader:
        records = (reader.forward(0, reader.cut().offset) if ascending
                   else reader.reverse(reader.cut().offset))
        observed = []
        for record in records:
            observed.append(record.entry.source_coordinates.id)
            assert reader.session_id == "original"
            reader.input_for(record)
    expected = [row["id"] for row in rows]
    assert observed == (expected if ascending else list(reversed(expected)))


@pytest.mark.parametrize("message", [None, [], "user", 3])
def test_parent_message_shape_is_rejected_at_its_existing_boundary(message):
    raw = json.dumps({"type": "message", "id": "parent", "message": message}).encode()
    with pytest.raises(ValueError, match="Expected PiMessage object"):
        NativeEntry.read_ancestry(raw, {"parent"})
