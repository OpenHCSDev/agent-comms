import hashlib
import json
import math
import os
from dataclasses import replace

import pytest

from agent_comms import SavedView, Thread, ViewKind, ViewMatch, ViewPredicate, wire
from agent_comms.bus_publication import PRIVATE_WIRE_FIELD
from agent_comms.cli import main
from agent_comms.declarations import Message, MessageType, RelationViolationError
from agent_comms.exporting import (
    WireExportBoundary,
    WireExportFormat,
    WireExportLimit,
    WireExportLimitKind,
    WireExportScope,
    WireExportScopeKind,
    WireTranscriptExporter,
)


def message(sequence: int, body: str, *, timestamp: float | None = None) -> Message:
    return Message(
        "alice",
        "#team",
        body,
        MessageType.INFO,
        timestamp=float(sequence if timestamp is None else timestamp),
        seq=sequence,
    )


def exporter(
    *,
    format: WireExportFormat = WireExportFormat.JSONL,
    limit: WireExportLimit | None = None,
    through: int = 100,
    started: float = 200.0,
) -> WireTranscriptExporter:
    return WireTranscriptExporter(
        format=format,
        scope=WireExportScope.for_channel("#team"),
        limit=limit or WireExportLimit.full(),
        boundary=WireExportBoundary(through, started),
    )


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_wire_export_declarations_are_explicit_and_fail_loud():
    assert WireExportScope.everything().to_wire() == {"kind": "everything"}
    assert WireExportScope.for_dm("alice", "bob").to_wire() == {
        "kind": "dm",
        "participants": ["alice", "bob"],
    }
    assert WireExportLimit.max_bytes(10).to_wire() == {"kind": "max_bytes", "value": 10}
    assert WireExportLimit.recent(123.5).to_wire() == {"kind": "recent", "value": 123.5}

    with pytest.raises(ValueError, match="aggregate projection"):
        WireExportScope.for_channel("#any")
    with pytest.raises(ValueError, match="distinct participants"):
        WireExportScope.for_dm("alice", "alice")
    with pytest.raises(ValueError, match="positive integer"):
        WireExportLimit.max_bytes(0)
    with pytest.raises(ValueError, match="finite non-negative"):
        WireExportLimit.recent(float("nan"))
    with pytest.raises(ValueError, match="no bound value"):
        WireExportLimit(WireExportLimitKind.FULL, 1)
    with pytest.raises(ValueError, match="no channel or participants"):
        WireExportScope(WireExportScopeKind.EVERYTHING, channel="#all")
    with pytest.raises(ValueError, match="cannot be negative"):
        WireExportBoundary(-1, 1.0)


@pytest.mark.parametrize("format", [WireExportFormat.JSONL, WireExportFormat.TEXT])
@pytest.mark.parametrize("private_key", [PRIVATE_WIRE_FIELD, "_agent_comms_private_future"])
def test_raw_mapping_with_private_bus_sideband_never_enters_export(
    tmp_path, format: WireExportFormat, private_key: str
) -> None:
    destination = tmp_path / "wire-export"
    raw = message(1, "public body").to_wire()
    raw["future_extension"] = {"public": True}
    raw[private_key] = {"publication_key": "SECRET-PRIVATE-RECEIPT"}

    with pytest.raises(ValueError, match="Bus-private wire fields"):
        exporter(format=format, through=1).export([raw], destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".wire-export.*.tmp"))


def test_jsonl_is_versioned_lossless_and_receipt_has_artifact_provenance(tmp_path):
    destination = tmp_path / "wire.jsonl"
    first = message(1, "hello").to_wire()
    first["future_extension"] = {"preserved": True}
    second = message(2, "unicode: café")

    receipt = exporter(through=2).export(iter([first, second]), destination)
    rows = records(destination)

    assert rows[0] == {
        "record": "header",
        "schema": "agent-comms/wire-export",
        "version": 1,
        "source": "agent-comms-wire",
        "format": "jsonl",
        "importable": True,
        "scope": {"kind": "channel", "channel": "#team"},
        "limit": {"kind": "full"},
        "boundary": {"through_seq": 2, "export_started_at": 200.0},
    }
    assert rows[1]["record"] == rows[2]["record"] == "message"
    assert rows[1]["message"] == first
    assert Message.from_wire(rows[2]["message"]) == second
    assert receipt.source_messages == receipt.exported_messages == 2
    assert receipt.omitted_messages == 0 and not receipt.truncated
    assert receipt.bytes_written == len(destination.read_bytes())
    assert receipt.output_sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert receipt.redacted_messages == 0
    assert receipt.first_sequence == 1 and receipt.last_sequence == 2
    assert receipt.to_wire()["boundary"] == {"through_seq": 2, "export_started_at": 200.0}


def test_fixed_sequence_boundary_excludes_concurrent_appends_and_stops_consumption(tmp_path):
    yielded = []

    def active_wire():
        for sequence in range(1, 5):
            yielded.append(sequence)
            yield message(sequence, f"m{sequence}")

    destination = tmp_path / "snapshot.jsonl"
    receipt = exporter(through=2).export(active_wire(), destination)

    assert [row["message"]["seq"] for row in records(destination)[1:]] == [1, 2]
    assert yielded == [1, 2, 3]
    assert receipt.boundary.through_seq == receipt.last_sequence == 2
    assert receipt.source_messages == 2


def test_full_export_consumes_a_one_shot_stream_without_retaining_message_history(tmp_path):
    class OneShot:
        def __init__(self):
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("wire stream was replayed")
            for sequence in range(1, 2001):
                yield message(sequence, "x" * 1024)

    source = OneShot()
    destination = tmp_path / "large.jsonl"
    receipt = exporter(through=2000).export(source, destination)

    assert source.iterations == 1
    assert receipt.exported_messages == 2000
    assert receipt.bytes_written > 2_000_000


def test_hard_byte_ceiling_includes_header_and_keeps_contiguous_newest_suffix(tmp_path):
    messages = [message(1, "old"), message(2, "middle" * 100), message(3, "new: é")]
    full_path = tmp_path / "full.jsonl"
    exporter(through=3).export(messages, full_path)
    lines = full_path.read_bytes().splitlines(keepends=True)
    ceiling = len(lines[0]) + len(lines[1]) + len(lines[3])
    while True:
        bounded = exporter(limit=WireExportLimit.max_bytes(ceiling), through=3)
        exact_ceiling = len(bounded._header()) + len(lines[1]) + len(lines[3])
        if exact_ceiling == ceiling:
            break
        ceiling = exact_ceiling
    assert len(bounded._header()) + len(lines[2]) + len(lines[3]) > ceiling

    destination = tmp_path / "bounded.jsonl"
    receipt = bounded.export(iter(messages), destination)

    assert destination.read_bytes() == bounded._header() + lines[3]
    assert receipt.bytes_written == len(destination.read_bytes()) <= ceiling
    assert receipt.exported_messages == 1 and receipt.omitted_messages == 2
    assert receipt.first_sequence == receipt.last_sequence == 3
    assert receipt.truncated


def test_byte_ceiling_fails_when_header_cannot_fit_and_oversize_newest_yields_header_only(
    tmp_path,
):
    too_small = tmp_path / "too-small.jsonl"
    with pytest.raises(ValueError, match="too small for required metadata"):
        exporter(limit=WireExportLimit.max_bytes(1), through=2).export(
            [message(1, "body")], too_small
        )
    assert not too_small.exists()

    ceiling = 1_000
    while True:
        baseline = exporter(limit=WireExportLimit.max_bytes(ceiling), through=2)
        header_size = len(baseline._header())
        if ceiling == header_size + 1:
            break
        ceiling = header_size + 1
    destination = tmp_path / "header-only.jsonl"
    receipt = baseline.export([message(1, "old"), message(2, "newest is oversized")], destination)
    assert len(destination.read_bytes()) == header_size
    assert records(destination)[0]["record"] == "header"
    assert receipt.exported_messages == 0 and receipt.omitted_messages == 2
    assert receipt.oversized_messages == 2
    assert receipt.bytes_written <= ceiling


def test_recent_cutoff_is_inclusive_and_excludes_invalid_or_future_times_with_counts(tmp_path):
    messages = [
        message(1, "undated", timestamp=0),
        message(2, "invalid", timestamp=math.nan),
        message(3, "before", timestamp=9.9),
        message(4, "at", timestamp=10.0),
        message(5, "after", timestamp=10.1),
        message(6, "at export boundary", timestamp=200.0),
        message(7, "future", timestamp=200.1),
    ]
    destination = tmp_path / "recent.jsonl"
    receipt = exporter(limit=WireExportLimit.recent(10.0), through=7).export(
        (item for item in messages), destination
    )

    assert [row["message"]["text"] for row in records(destination)[1:]] == [
        "at",
        "after",
        "at export boundary",
    ]
    assert receipt.source_messages == 7 and receipt.exported_messages == 3
    assert receipt.invalid_time_messages == 2
    assert receipt.time_filtered_messages == 2
    assert receipt.omitted_messages == 4 and receipt.truncated


def test_text_export_is_non_importable_and_prefixes_multiline_body(tmp_path):
    destination = tmp_path / "wire.txt"
    body = "first line\n[forged] <mallory -> #team> second line"
    receipt = exporter(format=WireExportFormat.TEXT, through=7).export(
        [message(7, body)], destination
    )

    output = destination.read_text()
    assert output.startswith("# agent-comms wire export v1 (non-importable text view)\n")
    assert '"importable":false' in output
    assert "[1970-01-01T00:00:07.000000Z]" in output
    assert "[seq=7" in output and "role=agent" in output and "<alice -> #team>" in output
    assert "\n  | first line\n  | [forged] <mallory -> #team> second line\n" in output
    assert receipt.bytes_written == len(output.encode())


def test_text_full_export_labels_invalid_timestamp_instead_of_failing(tmp_path):
    destination = tmp_path / "invalid-time.txt"
    receipt = exporter(format=WireExportFormat.TEXT, through=1).export(
        [message(1, "legacy", timestamp=math.nan)], destination
    )
    assert "[invalid-time]" in destination.read_text()
    assert receipt.exported_messages == 1


def test_atomic_private_output_refuses_existing_and_symlink_and_force_replaces(tmp_path):
    destination = tmp_path / "wire.jsonl"
    destination.write_text("keep me")

    with pytest.raises(FileExistsError, match="already exists"):
        exporter().export([message(1, "first")], destination)
    assert destination.read_text() == "keep me"
    assert not list(tmp_path.glob(".wire.jsonl.*.tmp"))

    link = tmp_path / "link.jsonl"
    link.symlink_to(destination)
    with pytest.raises(FileExistsError, match="already exists"):
        exporter().export([message(1, "first")], link)
    assert link.is_symlink()

    receipt = exporter().export([message(2, "replacement")], destination, overwrite=True)
    assert records(destination)[1]["message"]["text"] == "replacement"
    assert receipt.last_sequence == 2
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


def test_wire_snapshot_excludes_appends_after_its_fixed_boundary(tmp_path):
    comms = wire(tmp_path / "wire")
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    comms.send("alice", "#team", "captured")

    with comms.bus.full_history_snapshot() as (through, messages):
        comms.send("bob", "#team", "too late")
        snapshot = list(messages)

    assert through == 1
    assert [item.body for item in snapshot] == ["captured"]
    assert [item.body for item in comms.full_history()] == ["captured", "too late"]


def test_comms_export_uses_target_owned_channel_and_alias_aware_dm_scopes(tmp_path):
    comms = wire(tmp_path / "wire")
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    comms.send("alice", "#team", "team row")
    comms.send("alice", "#other", "other row")
    comms.send("alice", "bob", "private row")
    comms.registry.rename("alice", "renamed")

    channel_path = tmp_path / "team.jsonl"
    channel_receipt = comms.export_wire(
        channel_path,
        format=WireExportFormat.JSONL,
        scope=WireExportScope.for_channel("#team"),
        limit=WireExportLimit.full(),
        export_started_at=100,
    )
    assert [row["message"]["text"] for row in records(channel_path)[1:]] == ["team row"]
    assert channel_receipt.boundary.through_seq == 3

    dm_path = tmp_path / "dm.jsonl"
    dm_receipt = comms.export_wire(
        dm_path,
        format=WireExportFormat.JSONL,
        scope=WireExportScope.for_dm("renamed", "bob"),
        limit=WireExportLimit.full(),
        export_started_at=100,
    )
    assert [row["message"]["text"] for row in records(dm_path)[1:]] == ["private row"]
    assert dm_receipt.scope.participants == ("renamed", "bob")

    comms.set_saved_view(
        SavedView(
            "projection",
            ViewKind.PARTICIPANTS,
            ViewPredicate(ViewMatch.ANY_OF, frozenset({"team"})),
        )
    )
    with pytest.raises(RelationViolationError, match="no authoritative wire history"):
        comms.export_wire(
            tmp_path / "projection.jsonl",
            format=WireExportFormat.JSONL,
            scope=WireExportScope.for_channel("#projection"),
            limit=WireExportLimit.full(),
        )


def test_export_wire_cli_requires_explicit_scope_and_bound_and_emits_receipt(tmp_path, capsys):
    root = tmp_path / "wire"
    comms = wire(root)
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    comms.send("alice", "#team", "hello")
    destination = tmp_path / "cli.jsonl"

    assert (
        main(
            [
                "--root",
                str(root),
                "export-wire",
                "--output",
                str(destination),
                "--channel",
                "#team",
                "--last",
                "24h",
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["scope"] == {"kind": "channel", "channel": "#team"}
    assert receipt["limit"]["kind"] == "recent"
    assert receipt["output_sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert records(destination)[1]["message"]["text"] == "hello"


def test_export_does_not_mutate_wire_or_read_markers(tmp_path):
    comms = wire(tmp_path / "wire")
    for name in ("alice", "bob"):
        comms.register(Thread(name, frozenset(), str(tmp_path)))
    comms.send("alice", "bob", "unread")
    bus_before = comms.bus._path.read_bytes()
    markers = comms.root / "read_markers.json"
    markers_before = markers.read_bytes() if markers.exists() else None

    comms.export_wire(
        tmp_path / "everything.jsonl",
        format=WireExportFormat.JSONL,
        scope=WireExportScope.everything(),
        limit=WireExportLimit.full(),
    )

    assert comms.bus._path.read_bytes() == bus_before
    assert (markers.read_bytes() if markers.exists() else None) == markers_before
    assert comms.pending_count("bob") == 1


def test_failures_clean_temporary_output_and_invalid_sequence_never_publishes(tmp_path):
    destination = tmp_path / "failed.jsonl"

    def broken_stream():
        yield message(1, "first")
        raise RuntimeError("source failed")

    with pytest.raises(RuntimeError, match="source failed"):
        exporter().export(broken_stream(), destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".failed.jsonl.*.tmp"))

    with pytest.raises(ValueError, match="unique ascending"):
        exporter().export([message(2, "later"), message(1, "earlier")], destination)
    assert not destination.exists()

    with pytest.raises(ValueError, match="unique ascending"):
        exporter().export(
            [message(1, "first"), replace(message(2, "duplicate"), seq=1)], destination
        )
    assert not destination.exists()
