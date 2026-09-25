"""The disposable page index must follow the JSONL authority."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from agent_comms import Message, MessageBus, MessageType, Thread, ThreadRegistry
from agent_comms.declarations import _iter_jsonl_records


def _bus(tmp_path) -> MessageBus:
    registry = ThreadRegistry(tmp_path / "registry.json")
    for name in ("a", "b", "c"):
        registry.register(Thread(name, frozenset(), str(tmp_path)))
    return MessageBus(tmp_path / "bus.jsonl", registry)


def _rows(count: int, *, first: int = 1) -> bytes:
    return b"".join(
        (
            json.dumps(
                Message(
                    "a", "b" if seq % 3 == 0 else "c", f"m{seq}", MessageType.INFO, seq=seq
                ).to_wire()
            )
            + "\n"
        ).encode()
        for seq in range(first, first + count)
    )


def test_warm_incoming_cursor_reads_only_selected_wire_rows(tmp_path, monkeypatch):
    bus = _bus(tmp_path)
    bus._path.write_bytes(_rows(3000))
    assert bus.incoming_page("b", after=2700, limit=10).newest_seq == 2730
    decoded = 0
    original = bus._public_page_record

    def count(record, size):
        nonlocal decoded
        decoded += 1
        return original(record, size)

    monkeypatch.setattr(bus, "_public_page_record", count)
    page = bus.incoming_page("b", after=2700, limit=10)
    assert [message.seq for message in page.messages] == list(range(2703, 2731, 3))
    assert page.has_older and page.has_newer
    assert decoded <= 12  # previous match, ten results, next match


def test_index_rebuilds_after_append_replace_and_truncated_tail(tmp_path):
    bus = _bus(tmp_path)
    bus._path.write_bytes(_rows(9))
    assert [m.seq for m in bus.incoming_page("b", after=0).messages] == [3, 6, 9]
    with bus._path.open("ab") as output:
        output.write(_rows(3, first=10))
        output.flush()
        os.fsync(output.fileno())
    assert [m.seq for m in bus.incoming_page("b", after=9).messages] == [12]

    replacement = bus._path.with_suffix(".new")
    replacement.write_bytes(_rows(6, first=101))
    os.replace(replacement, bus._path)
    assert [m.seq for m in bus.incoming_page("b", after=0).messages] == [102, 105]

    with bus._path.open("ab") as output:
        output.write(b'{"seq": 107, "from": "a"')
    assert [m.seq for m in bus.incoming_page("b", after=0).messages] == [102, 105]


def test_bad_cached_offset_falls_back_to_authoritative_log(tmp_path):
    bus = _bus(tmp_path)
    bus._path.write_bytes(_rows(9))
    assert bus.incoming_page("b", after=6).newest_seq == 9
    with sqlite3.connect(tmp_path / "bus_page_index.sqlite3") as connection:
        connection.execute("UPDATE rows SET offset=0 WHERE seq=9")
    page = bus.incoming_page("b", after=6)
    assert [message.seq for message in page.messages] == [9]


def test_zero_sequence_legacy_row_counts_as_older_than_after_zero(tmp_path):
    bus = _bus(tmp_path)
    rows = [
        Message("a", "b", "legacy", MessageType.INFO, seq=0),
        Message("a", "b", "new", MessageType.INFO, seq=1),
    ]
    bus._path.write_bytes(b"".join((json.dumps(row.to_wire()) + "\n").encode() for row in rows))
    page = bus.incoming_page("b", after=0)
    assert [message.seq for message in page.messages] == [1]
    assert page.has_older is True


def test_index_does_not_reorder_nonmonotonic_legacy_wire(tmp_path):
    bus = _bus(tmp_path)
    rows = [
        Message("a", "b", "second", MessageType.INFO, seq=2),
        Message("a", "b", "first", MessageType.INFO, seq=1),
    ]
    bus._path.write_bytes(b"".join((json.dumps(row.to_wire()) + "\n").encode() for row in rows))
    with pytest.raises(ValueError, match="unique messages in seq order"):
        bus.full_history_page()


def test_indexed_pages_equal_scan_oracle_across_cursors_and_budgets(tmp_path):
    bus = _bus(tmp_path)
    bus._path.write_bytes(_rows(30))
    matches = lambda message: message.target == "b"
    for before, after in ((None, None), (18, None), (1, None), (None, 0), (None, 17)):
        for limit, max_bytes in ((1, 20), (3, 120), (100, 10000)):
            oracle = bus._collect_history_page(
                (
                    bus._public_page_record(record, size)
                    for record, size in _iter_jsonl_records(bus._path)
                ),
                matches,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
            )
            indexed = bus._history_page(
                matches,
                before=before,
                after=after,
                limit=limit,
                max_bytes=max_bytes,
                targets=frozenset({"b"}),
            )
            assert indexed == oracle
