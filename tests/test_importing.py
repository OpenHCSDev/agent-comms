import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from agent_comms import ImportFormat, ImportLimits, ThreadStatus, wire
from agent_comms.cli import main


def opencode_export(path, project):
    path.write_text(
        json.dumps(
            {
                "info": {"id": "ses_test", "title": "Current task", "directory": str(project)},
                "messages": [
                    {
                        "info": {"id": "summary", "role": "assistant", "summary": True},
                        "parts": [{"type": "text", "text": "Keep model authority centralized."}],
                    },
                    {
                        "info": {"id": "user", "role": "user"},
                        "parts": [{"type": "text", "text": "Continue the refactor"}],
                    },
                    {
                        "info": {"id": "assistant", "role": "assistant"},
                        "parts": [
                            {
                                "type": "tool",
                                "tool": "bash",
                                "callID": "call",
                                "state": {
                                    "status": "completed",
                                    "input": {"command": "pytest"},
                                    "output": "42 passed",
                                },
                            },
                            {
                                "type": "text",
                                "text": "Tests passed; the remaining work is UI routing.",
                            },
                            {"type": "file", "filename": "screenshot.png"},
                        ],
                    },
                ],
            }
        )
    )


@pytest.mark.parametrize("legacy_directory", [False, True])
def test_opencode_snapshot_is_stopped_resumable_and_source_unchanged(tmp_path, legacy_directory):
    source = tmp_path / "export.json"
    opencode_export(source, tmp_path)
    before = source.read_bytes()
    comms = wire(tmp_path / "wire")
    directory = comms.root / "imported_sessions"
    if legacy_directory:
        directory.mkdir(parents=True)
        directory.chmod(0o755)
    receipt = comms.import_thread(source, ImportFormat.OPENCODE, name="imported")
    thread = comms.registry.require("imported")
    assert thread.pid == 0 and comms.registry.status(thread.name) is ThreadStatus.STOPPED
    assert receipt.source_id == "ses_test" and receipt.imported_messages == 4
    assert source.read_bytes() == before
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(Path(thread.session_file).stat().st_mode) == 0o600
    records = [json.loads(line) for line in Path(thread.session_file).read_text().splitlines()]
    assert records[0]["type"] == "session" and records[0]["version"] == 3
    parent = None
    for record in records[1:]:
        assert record["parentId"] == parent
        parent = record["id"]
    context = "\n".join(event.text for event in comms.thread_transcript(thread.name))
    assert "Keep model authority" in context and "42 passed" in context
    assert "remaining work" in context
    with pytest.raises(ValueError, match="reserved"):
        comms.import_thread(source, ImportFormat.OPENCODE, name="imported")


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and symlinks")
def test_import_refuses_redirected_legacy_session_directory(tmp_path):
    source = tmp_path / "export.json"
    opencode_export(source, tmp_path)
    comms = wire(tmp_path / "wire")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (comms.root / "imported_sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="owner-controlled"):
        comms.import_thread(source, ImportFormat.OPENCODE, name="imported")
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert not list(outside.iterdir())
    assert not comms.registry.name_reserved("imported")


def test_codex_uses_response_items_not_mirrored_events_and_keeps_recent_budget(tmp_path):
    source = tmp_path / "rollout.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "codex-id", "cwd": str(tmp_path)}}]
    for index in range(10):
        records.extend(
            [
                {"type": "event_msg", "payload": {"type": "user_message", "message": "duplicate"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"Message {index}"}],
                    },
                },
            ]
        )
    records += [
        {"type": "compacted", "payload": {"message": "Earlier context summary"}},
        {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "opaque"}},
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec", "arguments": "{}"},
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 400}},
    ]
    source.write_text("".join(json.dumps(record) + "\n" for record in records) + '{"partial"')
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(4, 300, 100), None)
    assert len(snapshot.messages) <= 4 and sum(len(m.body) for m in snapshot.messages) <= 300
    assert snapshot.truncated_messages == 1
    assert snapshot.summary == "Earlier context summary"
    assert "duplicate" not in str(snapshot.messages) and "opaque" not in str(snapshot.messages)
    assert snapshot.notices
    tail = ImportFormat.CODEX.read(source, ImportLimits(1, 100, 100), None)
    assert len(tail.messages) == 1 and tail.messages[0].role.value == "tool"
    assert tail.latest_request == "Message 9"
    assert "Message 9" in tail.pi_session(tmp_path)


def test_codex_reads_latest_checkpoint_without_replaying_huge_prefix(tmp_path, monkeypatch):
    source = tmp_path / "large-rollout.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {
            "id": "child-current",
            "session_id": "parent-inherited",
            "cwd": str(tmp_path),
        },
    }
    obsolete = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "obsolete prefix"}],
        },
    }
    checkpoint = {
        "type": "compacted",
        "payload": {
            "message": "",
            "replacement_history": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Retained request"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Retained state"}],
                },
            ],
        },
    }
    tail = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Post-checkpoint result"}],
        },
    }
    with source.open("wb") as stream:
        stream.write((json.dumps(meta) + "\n").encode())
        encoded = (json.dumps(obsolete) + "\n").encode()
        for _ in range(40_000):
            stream.write(encoded)
        stream.write((json.dumps(checkpoint) + "\n").encode())
        stream.write((json.dumps(tail) + "\n").encode())

    original_open = Path.open
    opened = []

    class CountingReader:
        def __init__(self, stream):
            self.stream = stream
            self.bytes_read = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def seek(self, *args):
            return self.stream.seek(*args)

        def tell(self):
            return self.stream.tell()

        def read(self, size=-1):
            value = self.stream.read(size)
            self.bytes_read += len(value)
            return value

        def readline(self, size=-1):
            value = self.stream.readline(size)
            self.bytes_read += len(value)
            return value

    def tracked_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == source and args and args[0] == "rb":
            reader = CountingReader(stream)
            opened.append(reader)
            return reader
        return stream

    monkeypatch.setattr(Path, "open", tracked_open)
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(), "child-current")
    assert snapshot.source_id == "child-current"
    assert [message.body for message in snapshot.messages] == [
        "Retained request",
        "Retained state",
        "Post-checkpoint result",
    ]
    assert snapshot.latest_request == "Retained request"
    assert "obsolete prefix" not in str(snapshot.messages)
    assert opened[0].bytes_read < 200_000
    with pytest.raises(ValueError, match="different"):
        ImportFormat.CODEX.read(source, ImportLimits(), "parent-inherited")


def test_codex_fixed_boundary_ignores_concurrent_append(tmp_path, monkeypatch):
    source = tmp_path / "active-rollout.jsonl"
    source.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "active", "cwd": str(tmp_path)}})
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "At boundary"}],
                },
            }
        )
        + "\n"
    )
    appended = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Appended too late"}],
        },
    }
    original_open = Path.open

    class AppendAfterBoundary:
        def __init__(self, stream):
            self.stream = stream
            self.appended = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def seek(self, offset, whence=0):
            result = self.stream.seek(offset, whence)
            if whence == 2 and not self.appended:
                self.appended = True
                with original_open(source, "ab") as writer:
                    writer.write((json.dumps(appended) + "\n").encode())
            return result

        def tell(self):
            return self.stream.tell()

        def read(self, size=-1):
            return self.stream.read(size)

        def readline(self, size=-1):
            return self.stream.readline(size)

    def active_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == source and args and args[0] == "rb":
            return AppendAfterBoundary(stream)
        return stream

    monkeypatch.setattr(Path, "open", active_open)
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(), None)
    assert [message.body for message in snapshot.messages] == ["At boundary"]
    assert "Appended too late" not in str(snapshot.messages)


def test_codex_without_checkpoint_keeps_chronological_bounded_suffix(tmp_path):
    source = tmp_path / "uncompacted-rollout.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "plain", "cwd": str(tmp_path)}}]
    for index in range(3):
        records.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": f"Request {index}"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"Answer {index}"}],
                    },
                },
            ]
        )
    source.write_text("".join(json.dumps(record) + "\n" for record in records) + '{"partial"')
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(2, 1_000, 100), None)
    assert [message.body for message in snapshot.messages] == ["Request 2", "Answer 2"]
    assert snapshot.messages_seen == 6
    assert snapshot.latest_request == "Request 2"
    assert snapshot.notices == ("An incomplete final source record was omitted.",)


def test_codex_reverse_budget_retains_one_contiguous_newest_suffix(tmp_path):
    source = tmp_path / "budgeted-rollout.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "budget", "cwd": str(tmp_path)}}]
    for body in ("o" * 10, "m" * 80, "n" * 60):
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": body}],
                },
            }
        )
    source.write_text("".join(json.dumps(record) + "\n" for record in records))
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(10, 100, 100), None)
    assert [message.body for message in snapshot.messages] == ["n" * 60]
    assert snapshot.messages_seen == 3


def test_codex_selects_only_latest_of_several_compactions(tmp_path):
    source = tmp_path / "several-compactions.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": "compactions", "cwd": str(tmp_path)}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Obsolete request"}],
            },
        },
        {
            "type": "compacted",
            "payload": {
                "message": "Obsolete summary",
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Old checkpoint"}],
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Old tail"}],
            },
        },
        {
            "type": "compacted",
            "payload": {
                "message": "Current summary",
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Current checkpoint"}],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Current state"}],
                    },
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Current tail"}],
            },
        },
    ]
    source.write_text("".join(json.dumps(record) + "\n" for record in records))
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(), None)
    assert snapshot.summary == "Current summary"
    assert [message.body for message in snapshot.messages] == [
        "Current checkpoint",
        "Current state",
        "Current tail",
    ]
    assert snapshot.latest_request == "Current checkpoint"
    assert "Old" not in str(snapshot.messages) and "Obsolete" not in str(snapshot.messages)


def test_codex_compaction_record_may_span_several_reverse_blocks(tmp_path):
    source = tmp_path / "large-compaction-record.jsonl"
    large_state = "x" * 150_000
    records = [
        {"type": "session_meta", "payload": {"id": "large-record", "cwd": str(tmp_path)}},
        {
            "type": "compacted",
            "payload": {
                "message": "Large checkpoint",
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": large_state}],
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Resume after large state"}],
                    },
                ],
            },
        },
    ]
    source.write_text("".join(json.dumps(record) + "\n" for record in records))
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(10, 200_000, 160_000), None)
    assert snapshot.summary == "Large checkpoint"
    assert snapshot.messages[0].body == large_state
    assert snapshot.messages[1].body == "Resume after large state"


def test_latest_request_is_not_duplicated_when_retained(tmp_path):
    source = tmp_path / "rollout.jsonl"
    source.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "dedupe", "cwd": str(tmp_path)}})
        + "\n"
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Continue exactly once"}],
                },
            }
        )
        + "\n"
    )
    snapshot = ImportFormat.CODEX.read(source, ImportLimits(), None)
    assert snapshot.pi_session(tmp_path).count("Continue exactly once") == 1


def test_opencode_database_uses_readonly_snapshot(tmp_path):
    source = tmp_path / "opencode.db"
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE session(id TEXT, directory TEXT, title TEXT, revert TEXT);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INT, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, time_created INT, data TEXT);
        """)
        db.execute("INSERT INTO session VALUES(?,?,?,NULL)", ("session", str(tmp_path), "Title"))
        db.execute(
            "INSERT INTO message VALUES(?,?,?,?)", ("message", "session", 1, '{"role":"user"}')
        )
        db.execute(
            "INSERT INTO part VALUES(?,?,?,?)",
            ("part", "message", 1, '{"type":"text","text":"Resume this task"}'),
        )
    before = source.read_bytes()
    snapshot = ImportFormat.OPENCODE.read(source, ImportLimits(), "session")
    assert snapshot.messages[0].body == "Resume this task"
    assert source.read_bytes() == before
    with pytest.raises(ValueError, match="session-id"):
        ImportFormat.OPENCODE.read(source, ImportLimits(), None)


def test_import_cli_and_invalid_sources(tmp_path, capsys):
    source = tmp_path / "export.json"
    opencode_export(source, tmp_path)
    assert (
        main(
            [
                "--root",
                str(tmp_path / "wire"),
                "import-thread",
                "--format",
                "opencode",
                "--source",
                str(source),
                "--name",
                "copied",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["thread"] == "copied"
    with pytest.raises(ValueError, match="different"):
        ImportFormat.OPENCODE.read(source, ImportLimits(), "wrong")
    with pytest.raises(ValueError, match="positive"):
        ImportLimits(messages=0)
    source.write_text('{"type":"session_meta","payload":{}}\ninvalid\n')
    with pytest.raises(ValueError, match="Malformed"):
        ImportFormat.CODEX.read(source, ImportLimits(), None)
