
from agent_comms.bus import Bus
import asyncio
import json
import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.importing import ImportLimits
from agent_comms.cli import main
from agent_comms.cli_bridges.claude import ClaudeBridge, ClaudeTranscript, RunningClaude
from agent_comms.cli_bridges.codex import CodexAppServer, CodexBridge
from agent_comms.cli_bridges.codex_rollout import ROLLOUT_RECORD, ForeignItem, ForeignRecord
from agent_comms.cli_bridges.history import (
    AnySession, DefaultAddress, History, NamedSession, OfflineHistory, OnlineHistory,
)
from agent_comms.cli_bridges.opencode import OpenCodeBridge, OpenCodeServer
from agent_comms.cli_bridges.opencode_parts import SavedMessage
from agent_comms.cli_bridges.opencode_protocol import Session
from agent_comms.cli_bridges.opencode_store import OpenCodeDatabase, OpenCodeExport
from agent_comms.field_codec import FieldCodec
from agent_comms.harness_install import ClaudeCodeHarness, CodexHarness, OpenCodeHarness
from agent_comms.importing import ImportBuffer, ThreadImported, ImportSnapshot, ImportedSessionMetadata
from agent_comms.native_entries import NativeEntry, ImportedMetadataEntry
from agent_comms.thread_identity import ThreadProvenance
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.runtime_updates import FinishedItem
from agent_comms.transcript_events import ImportedReply, ImportedRequest, TextTranscript
from agent_comms.ui_model.message_category import AgentCategory, UserCategory
from agent_comms.turn_context import CodexRolloutProvenance
from agent_comms.thread_text import GivenThreadText, NoThreadText


@pytest.fixture
def comms(comms):
    Bus.install(Bus.path_in(comms.root))
    return comms


def lines(path, records, tail=""):
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records) + tail)
    return path


def codex_lines(path, records, tail=""):
    return lines(path, ({"timestamp": "2026-10-18T00:00:00Z", **record} for record in records), tail)


def read(harness, source, limits=ImportLimits(), session=AnySession()):
    return ImportSnapshot.read(OfflineHistory.of(harness, source, session), limits)


def bodies(snapshot):
    return [message.body for message in snapshot.messages]


@pytest.mark.parametrize("title,display,renamed_display", [
    (NoThreadText(), "original", "renamed"),
    (GivenThreadText("Authored title"), "Authored title", "Authored title"),
])
def test_thread_display_title_is_inherited_without_copying_the_name(tmp_path, title, display, renamed_display, *, thread_registration):
    thread = thread_registration.new_thread('original', frozenset(), str(tmp_path), title=title)
    captured = ThreadProvenance.capture(thread)
    assert thread.title == captured.title == title
    assert thread.display_title == captured.display_title == display
    renamed = replace(thread, name="renamed")
    assert renamed.title == title
    assert renamed.display_title == renamed_display
    encoded = thread.to_wire()
    assert "display_title" not in encoded
    assert encoded["title"] == FieldCodec.encode(title)
    assert FieldCodec.decode(type(thread), encoded).title == title
    assert FieldCodec.encode(NoThreadText()) is None


def test_every_harness_has_one_offline_family_and_one_online_reader():
    # Readers are found by crossing the harness parent with offline/online; a bridge is
    # its harness's online reader plus following, declared outside the family.
    assert not {ClaudeBridge, CodexBridge, OpenCodeBridge} & set(History.members_with(History))
    assert OfflineHistory.readers(ClaudeCodeHarness) == (ClaudeTranscript,)
    assert OfflineHistory.readers(CodexHarness)[0].__name__ == "CodexRollout"
    assert set(OfflineHistory.readers(OpenCodeHarness)) == {OpenCodeExport, OpenCodeDatabase}
    assert OnlineHistory.readers(ClaudeCodeHarness) == (RunningClaude,)
    assert OnlineHistory.readers(CodexHarness) == (CodexAppServer,)
    assert OnlineHistory.readers(OpenCodeHarness) == (OpenCodeServer,)


# Claude Code


def claude(kind, uuid, **fields):
    record = {"type": kind, "uuid": uuid, "timestamp": "2026-10-18T00:00:00Z", "cwd": "/work/project",
              "sessionId": "session"}
    return record | fields


def claude_prompt(uuid, text, **fields):
    return claude("user", uuid, promptId="p-" + uuid, message={"role": "user", "content": text}, **fields)


def claude_reply(uuid, text):
    return claude("assistant", uuid, message={"content": [{"type": "text", "text": text}]})


def claude_tool(uuid, tool_id, name, arguments):
    return claude("assistant", uuid, message={"content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}]})


def claude_result(uuid, tool_id, text, error=False):
    return claude("user", uuid, promptId="p-" + uuid, message={"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": error}]})


def test_claude_subagent_keeps_its_task_its_commander_and_its_tools(tmp_path):
    session = tmp_path / "session.jsonl"
    lines(session, [claude_prompt("h", "hello", origin={"kind": "human"})])
    subagent = tmp_path / "session/subagents/agent-a1.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.with_suffix(".meta.json").write_text(json.dumps({"description": "Fix the importer"}))
    lines(subagent, [
        {"type": "fork-context-ref", "agentId": "a1", "parentSessionId": "session"},
        claude_prompt("t", "Your task: fix the importer"),
        {"type": "api-request-shape", "sessionId": "session", "shapeHash": "h", "shape": {"system": []}},
        {"type": "api-request-blob", "sessionId": "session", "hash": "b", "message": {"role": "user"}},
        {"type": "api-request", "sessionId": "session", "id": "q", "shapeHash": "h", "params": {}},
        claude_prompt("r", "<system-reminder>harness note</system-reminder>", isMeta=True),
        claude_tool("c", "g1", "Glob", {}),
        claude_result("e", "g1", "pattern is required", error=True),
        claude_tool("c2", "b1", "Bash", {"command": "pytest"}),
        claude_result("o", "b1", "42 passed"),
        claude_prompt("m", "Also delete the old decoder", isMeta=True, origin={"kind": "coordinator"}),
        claude_reply("d", "Done."),
    ], tail='{"type":"assistant","uuid":"cut')
    snapshot = read(ClaudeCodeHarness, subagent)
    assert snapshot.source_id == "agent-a1" and snapshot.title == GivenThreadText("Fix the importer")
    assert snapshot.project == "/work/project"
    assert bodies(snapshot) == [
        "Your task: fix the importer",
        "[Historical tool call: Glob]\n{}",
        "[Historical tool result, failed]\npattern is required",
        '[Historical tool call: bash]\n{"command": "pytest"}',
        "[Historical tool result]\n42 passed",
        "Also delete the old decoder",
        "Done.",
    ]
    assert [type(message) for message in snapshot.messages][::5] == [ImportedRequest, ImportedRequest]
    assert snapshot.latest_request == "Also delete the old decoder"
    assert "harness note" not in str(snapshot.messages)
    assert set(snapshot.notices) == {
        "Context inherited from session session is not imported.",
        "An incomplete final source record was omitted.",
    }
    assert ClaudeTranscript.subagents(session) == (subagent,)


def claude_narration(uuid, text):
    """Claude Code saves narration and hidden reasoning under one tag; only narration has text."""
    return claude("assistant", uuid, message={"content": [
        {"type": "thinking", "thinking": text, "signature": "opaque"}]})


def test_claude_narration_before_a_tool_call_is_a_regular_message(tmp_path):
    source = lines(tmp_path / "s.jsonl", [
        claude_prompt("p", "Fix it", origin={"kind": "human"}),
        claude_narration("hidden", ""),
        claude_narration("n", "Reading the decoder first."),
        claude_tool("c", "b1", "Bash", {"command": "pytest"}),
        claude_result("o", "b1", "42 passed"),
        claude_reply("d", "Fixed."),
    ])
    events = [event for update in OfflineHistory.of(ClaudeCodeHarness, source, AnySession()).read()
              if isinstance(update, FinishedItem) for event in update.events
              if isinstance(event, TextTranscript)]
    assert [(event.text, event.category) for event in events] == [
        ("Fix it", UserCategory),
        ("Reading the decoder first.", AgentCategory),
        ("Fixed.", AgentCategory),
    ]
    snapshot = read(ClaudeCodeHarness, source)
    assert [(type(message), body) for message, body in zip(snapshot.messages, bodies(snapshot))
            if body in {"Reading the decoder first.", "Fixed."}] == [
        (ImportedReply, "Reading the decoder first."), (ImportedReply, "Fixed.")]


def test_claude_reads_from_its_latest_compaction(tmp_path):
    source = lines(tmp_path / "s.jsonl", [
        claude_prompt("old", "Obsolete request", origin={"kind": "human"}),
        claude_reply("old-r", "Obsolete reply"),
        claude("system", "b", subtype="compact_boundary"),
        claude_prompt("s", "Summary of the work so far", isCompactSummary=True),
        claude_reply("n", "Next step"),
    ])
    snapshot = read(ClaudeCodeHarness, source)
    assert snapshot.summary == "Summary of the work so far"
    assert bodies(snapshot) == ["Next step"]
    # The selected history has no request, so the newest earlier one is reported.
    assert snapshot.latest_request == "Obsolete request"
    context = json.loads(snapshot.pi_session(tmp_path).splitlines()[2])["message"]["content"][0]["text"]
    assert "Most recent user request in the source session:\nObsolete request" in context


def test_claude_refuses_damaged_lines_and_records_it_does_not_know(tmp_path):
    source = tmp_path / "s.jsonl"
    source.write_text(json.dumps(claude_prompt("a", "first", origin={"kind": "human"})) + "\n"
                      + '{"type":"user","uuid":"cut{"type":"assistant"}\n'
                      + json.dumps(claude_reply("b", "second")) + "\n")
    with pytest.raises(json.JSONDecodeError):
        read(ClaudeCodeHarness, source)
    lines(source, [claude_reply("b", "second"), {"type": "a-record-claude-adds-later"}])
    with pytest.raises(ValueError):
        read(ClaudeCodeHarness, source)


def test_running_claude_session_is_found_by_its_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / ".claude/projects/project/live-session.jsonl"
    source.parent.mkdir(parents=True)
    lines(source, [claude_prompt("a", "keep going", origin={"kind": "human"})])
    history = OnlineHistory.of(ClaudeCodeHarness, "live-session", DefaultAddress(), tmp_path)
    assert bodies(ImportSnapshot.read(history, ImportLimits())) == ["keep going"]
    with pytest.raises(ValueError):
        ImportSnapshot.read(OnlineHistory.of(ClaudeCodeHarness, "missing", DefaultAddress(), tmp_path), ImportLimits())


def test_import_subagents_names_each_thread_by_its_id(tmp_path, capsys, comms):
    session = lines(tmp_path / "session.jsonl", [claude_prompt("h", "hello", origin={"kind": "human"})])
    for name in ("a1", "b2"):
        subagent = tmp_path / f"session/subagents/agent-{name}.jsonl"
        subagent.parent.mkdir(parents=True, exist_ok=True)
        lines(subagent, [claude_prompt("t", f"task {name}", cwd=str(tmp_path))])
    assert main(["--root", str(comms.root), "import-subagents", "--session", str(session),
                 "--prefix", "unit-"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert sorted(item["thread"] for item in result["imported"]) == ["unit-a1", "unit-b2"]
    assert comms.registry.status("unit-a1") == StoppedThreadStatus()


# Codex


def codex_message(role, text):
    return {"type": "response_item", "timestamp": "2026-10-18T00:00:00Z", "payload": {
        "type": "message", "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


def codex_meta(identity, project):
    return {"type": "session_meta", "timestamp": "2026-10-18T00:00:00Z",
            "payload": {"id": identity, "cwd": str(project)}}


def test_codex_instructions_are_authenticated_historical_references_not_current_messages(tmp_path, capsys, comms):
    instructions = [
        {"type": "message", "role": "system", "content": "Historical base π.\n"},
        {"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": "Historical developer λ.\n"}]},
    ]
    source = codex_lines(tmp_path / "historical.jsonl", [
        codex_meta("original", tmp_path),
        {"type": "compacted", "payload": {"message": "Original summary",
         "replacement_history": instructions + [
             {"type": "message", "role": "user", "content": "Original question"}],
         "guardian_history": [{"type": "message", "role": "developer",
                               "content": "Private guardian wording"}]}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": "Original answer"}}])
    original = source.read_bytes()
    acknowledgement = comms.threads.import_thread(
        OfflineHistory.of(CodexHarness, source, AnySession()), name="historical")
    assert source.read_bytes() == original
    refs = acknowledgement.historical_instructions
    assert all(isinstance(ref, CodexRolloutProvenance) for ref in refs)
    assert tuple(ref.public_text(comms) for ref in refs) == (
        "Historical base π.\n", "Historical developer λ.\n")
    assert FieldCodec.decode(ThreadImported, acknowledgement.to_wire()) == acknowledgement
    assert acknowledgement.harness is CodexHarness
    saved = [json.loads(line) for line in Path(acknowledgement.session_file).read_text().splitlines()]
    metadata = next(record["data"] for record in saved if record["type"] == "custom")
    assert metadata["format"] == "codex"
    assert FieldCodec.decode(tuple[CodexRolloutProvenance, ...],
                             metadata["historical_instructions"]) == refs
    original_owner = comms.registry.require("historical")
    assert ImportedSessionMetadata.sources_for_owner(comms.registry, original_owner) == refs
    entry = NativeEntry.from_wire(saved[1])
    assert isinstance(entry, ImportedMetadataEntry)
    assert entry.to_wire() == saved[1]
    assert ImportedSessionMetadata.public_source_text(
        comms.registry, original_owner, refs[1], comms).text == "Historical developer λ.\n"
    with pytest.raises(ValueError):
        ImportedSessionMetadata.public_source_text(
            comms.registry, original_owner, replace(refs[1], instruction=999), comms)
    with pytest.raises(ValueError):
        ImportedSessionMetadata.sources_for_owner(
            comms.registry, replace(original_owner, created_at=original_owner.created_at + 1))
    with pytest.raises(ValueError):
        ImportedSessionMetadata.sources_for_owner(
            comms.registry, replace(original_owner, session_file=str(tmp_path / "other.jsonl")))
    assert main(["--root", str(comms.root), "context", "historical", "--imported"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert FieldCodec.decode(tuple[CodexRolloutProvenance, ...], result["sources"]) == refs
    assert result["scope"].startswith("historical-imported-instructions")
    # Native configuration records can precede the same original import metadata;
    # conversation remains outside this prefix-only metadata read.
    session_path = Path(acknowledgement.session_file)
    saved.insert(1, {"type": "model_change", "id": "native-config",
                     "timestamp": "2026-10-18T00:00:00Z",
                     "provider": "authored", "modelId": "authored"})
    session_path.write_text("".join(json.dumps(record) + "\n" for record in saved)
                            + "not part of the metadata prefix\n")
    assert ImportedSessionMetadata.sources_for_owner(comms.registry, original_owner) == refs
    messages = json.dumps([record for record in saved if record["type"] == "message"], ensure_ascii=False)
    assert "Historical base π." not in messages and "Historical developer λ." not in messages
    assert "Private guardian wording" not in Path(acknowledgement.session_file).read_text()
    assert "Original question" in messages and "Original answer" in messages
    # Appending future turns preserves the original exact record reference.
    with source.open("ab") as output:
        output.write(b'{"type":"future","payload":{}}\n')
    assert refs[1].public_text(comms) == "Historical developer λ.\n"
    source.write_bytes(source.read_bytes().replace("developer λ".encode(), "developer ψ".encode()))
    with pytest.raises(ValueError):
        refs[1].public_text(comms)


def test_codex_instructions_come_from_the_selected_history_and_the_request_from_before_it(tmp_path):
    source = codex_lines(tmp_path / "selected.jsonl", [
        codex_meta("original", tmp_path),
        codex_message("user", "Prior question"),
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": "Old cut only"}},
        {"type": "compacted", "payload": {"message": "Selected summary", "replacement_history": [
            {"type": "message", "role": "developer", "content": "Selected historical wording"}]}},
    ])
    snapshot = read(CodexHarness, source, session=NamedSession("original"))
    assert snapshot.latest_request == "Prior question"
    assert tuple(ref.public_text(None) for ref in snapshot.historical_instructions) == (
        "Selected historical wording",)


def test_codex_items_codex_does_not_declare_are_foreign():
    def item(payload):
        return ROLLOUT_RECORD.validate_python({"type": "response_item", "timestamp": "2026-10-18T00:00:00Z", "payload": payload})

    assert isinstance(item({"type": "developer", "role": "developer", "content": "x"}).payload, ForeignItem)
    assert isinstance(item({"type": "message", "role": "guardian", "content": "x"}).payload, ForeignItem)
    assert isinstance(item({"type": "reasoning", "encrypted_content": "opaque"}).payload, ForeignItem)
    assert isinstance(ROLLOUT_RECORD.validate_python({"type": "event_msg", "timestamp": "2026-10-18T00:00:00Z", "payload": {}}), ForeignRecord)


def test_codex_import_uses_requested_name_not_rollout_filename(tmp_path, comms):
    source = codex_lines(tmp_path / "rollout-2026-10-16T15-00-00-codex-id.jsonl", [
        codex_meta("codex-id", tmp_path), codex_message("user", "Continue editing the paper")])
    original = source.read_bytes()
    acknowledgement = comms.threads.import_thread(
        OfflineHistory.of(CodexHarness, source, AnySession()), name="paper4_jair", tags=frozenset({"papers"}))
    thread = comms.registry.require("paper4_jair")
    assert thread.name == thread.display_title == "paper4_jair"
    assert thread.tags == frozenset({"papers"})
    assert comms.registry.status(thread.name) == StoppedThreadStatus()
    assert acknowledgement.source_id == "codex-id"
    assert source.read_bytes() == original


def test_codex_uses_response_items_not_mirrored_events_and_keeps_recent_budget(tmp_path):
    records = [codex_meta("codex-id", tmp_path)]
    for index in range(10):
        records += [{"type": "event_msg", "payload": {"type": "user_message", "message": "duplicate"}},
                    codex_message("user", f"Message {index}")]
    records += [
        {"type": "compacted", "payload": {"message": "Earlier context summary"}},
        {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "opaque"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec", "arguments": "{}"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 400}},
    ]
    source = codex_lines(tmp_path / "rollout.jsonl", records, tail='{"partial"')
    snapshot = read(CodexHarness, source, ImportLimits(4, 300, 100))
    assert len(snapshot.messages) <= 4 and sum(len(m.body) for m in snapshot.messages) <= 300
    assert snapshot.truncated_messages == 1
    assert snapshot.summary == "Earlier context summary"
    assert "duplicate" not in str(snapshot.messages) and "opaque" not in str(snapshot.messages)
    assert snapshot.notices == ("An incomplete final source record was omitted.",)
    tail = read(CodexHarness, source, ImportLimits(1, 100, 100))
    assert len(tail.messages) == 1 and tail.messages[0].body.startswith("[Historical tool result]")
    assert tail.latest_request == "Message 9"
    assert "Message 9" in tail.pi_session(tmp_path)


class CountingReader:
    def __init__(self, stream, after_end=None):
        self.stream = stream
        self.bytes_read = 0
        self.after_end = after_end

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def seek(self, offset, whence=0):
        result = self.stream.seek(offset, whence)
        if whence == 2 and self.after_end is not None:
            self.after_end(), setattr(self, "after_end", None)
        return result

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


def watch_reads(monkeypatch, source, after_end=None):
    original_open = Path.open
    opened = []

    def tracked_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == source and args and args[0] == "rb":
            opened.append(CountingReader(stream, after_end))
            return opened[-1]
        return stream

    monkeypatch.setattr(Path, "open", tracked_open)
    return opened


def test_codex_reads_latest_checkpoint_without_replaying_huge_prefix(tmp_path, monkeypatch):
    source = tmp_path / "large-rollout.jsonl"
    meta = {"type": "session_meta", "timestamp": "2026-10-18T00:00:00Z",
            "payload": {"id": "child-current", "session_id": "parent-inherited",
                                                "cwd": str(tmp_path)}}
    checkpoint = {"type": "compacted", "timestamp": "2026-10-18T00:00:00Z",
                  "payload": {"message": "", "replacement_history": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Retained request"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Retained state"}]},
    ]}}
    with source.open("wb") as stream:
        stream.write((json.dumps(meta) + "\n").encode())
        encoded = (json.dumps(codex_message("assistant", "obsolete prefix")) + "\n").encode()
        for _ in range(40_000):
            stream.write(encoded)
        stream.write((json.dumps(checkpoint) + "\n").encode())
        stream.write((json.dumps(codex_message("assistant", "Post-checkpoint result")) + "\n").encode())
    opened = watch_reads(monkeypatch, source)
    snapshot = read(CodexHarness, source, session=NamedSession("child-current"))
    assert snapshot.source_id == "child-current"
    assert bodies(snapshot) == ["Retained request", "Retained state", "Post-checkpoint result"]
    assert snapshot.latest_request == "Retained request"
    assert "obsolete prefix" not in str(snapshot.messages)
    assert opened[0].bytes_read < 200_000
    with pytest.raises(ValueError):
        read(CodexHarness, source, session=NamedSession("parent-inherited"))


def test_codex_fixed_boundary_ignores_concurrent_append(tmp_path, monkeypatch):
    source = codex_lines(tmp_path / "active-rollout.jsonl", [codex_meta("active", tmp_path), codex_message("user", "At boundary")])

    def append():
        with source.open("ab") as writer:
            writer.write((json.dumps(codex_message("assistant", "Appended too late")) + "\n").encode())

    watch_reads(monkeypatch, source, append)
    snapshot = read(CodexHarness, source)
    assert bodies(snapshot) == ["At boundary"]
    assert "Appended too late" in source.read_text()


def test_codex_without_checkpoint_keeps_chronological_bounded_suffix(tmp_path):
    records = [codex_meta("plain", tmp_path)]
    for index in range(3):
        records += [codex_message("user", f"Request {index}"), codex_message("assistant", f"Answer {index}")]
    source = codex_lines(tmp_path / "uncompacted-rollout.jsonl", records, tail='{"partial"')
    snapshot = read(CodexHarness, source, ImportLimits(2, 1_000, 100))
    assert bodies(snapshot) == ["Request 2", "Answer 2"]
    assert snapshot.messages_seen == 6
    assert snapshot.latest_request == "Request 2"
    assert snapshot.notices == ("An incomplete final source record was omitted.",)


def test_bounded_suffix_is_one_contiguous_newest_run(tmp_path):
    source = codex_lines(tmp_path / "budgeted-rollout.jsonl", [codex_meta("budget", tmp_path)] + [
        codex_message("assistant", body) for body in ("o" * 10, "m" * 80, "n" * 60)])
    snapshot = read(CodexHarness, source, ImportLimits(10, 100, 100))
    assert bodies(snapshot) == ["n" * 60]
    assert snapshot.messages_seen == 3


def test_codex_selects_only_latest_of_several_compactions(tmp_path):
    def compacted(summary, *items):
        return {"type": "compacted", "payload": {"message": summary, "replacement_history": [
            record["payload"] for record in items]}}

    source = codex_lines(tmp_path / "several-compactions.jsonl", [
        codex_meta("compactions", tmp_path),
        codex_message("user", "Obsolete request"),
        compacted("Obsolete summary", codex_message("user", "Old checkpoint")),
        codex_message("assistant", "Old tail"),
        compacted("Current summary", codex_message("user", "Current checkpoint"),
                  codex_message("assistant", "Current state")),
        codex_message("assistant", "Current tail"),
    ])
    snapshot = read(CodexHarness, source)
    assert snapshot.summary == "Current summary"
    assert bodies(snapshot) == ["Current checkpoint", "Current state", "Current tail"]
    assert snapshot.latest_request == "Current checkpoint"
    assert "Old" not in str(snapshot.messages) and "Obsolete" not in str(snapshot.messages)


def test_codex_compaction_record_may_span_several_reverse_blocks(tmp_path):
    large_state = "x" * 150_000
    source = codex_lines(tmp_path / "large-compaction-record.jsonl", [
        codex_meta("large-record", tmp_path),
        {"type": "compacted", "payload": {"message": "Large checkpoint", "replacement_history": [
            codex_message("assistant", large_state)["payload"],
            codex_message("user", "Resume after large state")["payload"]]}},
    ])
    snapshot = read(CodexHarness, source, ImportLimits(10, 200_000, 160_000))
    assert snapshot.summary == "Large checkpoint"
    assert bodies(snapshot) == [large_state, "Resume after large state"]


def test_latest_request_is_not_duplicated_when_retained(tmp_path):
    source = codex_lines(tmp_path / "rollout.jsonl", [codex_meta("dedupe", tmp_path),
                                                codex_message("user", "Continue exactly once")])
    assert read(CodexHarness, source).pi_session(tmp_path).count("Continue exactly once") == 1


def test_codex_rollout_must_start_with_its_session_and_decode_completely(tmp_path):
    source = tmp_path / "rollout.jsonl"
    source.write_text(json.dumps(codex_message("user", "no metadata")) + "\n")
    with pytest.raises(ValueError):
        read(CodexHarness, source)
    source.write_text('{"type":"session_meta","payload":{}}\n')
    with pytest.raises(ValueError):
        read(CodexHarness, source)


class FakeCodex:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def call(self, params):
        self.calls.append(params)
        if params.method == "thread/resume":
            return {"thread": {"status": {"type": "idle"}, "cwd": "/work/codex"}}
        return self.pages[params.cursor]


def codex_entry(turn, item):
    return {"turnId": turn, "item": item, "startedAtMs": 1, "completedAtMs": 2}


def test_codex_app_server_reads_only_newest_page_and_keeps_bus_inputs():
    from agent_comms.cli_bridges.codex_protocol import BusInput

    bus = BusInput.for_message("thread", "m1", "from the bus")
    newest = {"data": [
        codex_entry("t2", {"type": "agentMessage", "id": "i3", "text": "second answer"}),
        codex_entry("t2", {"type": "userMessage", "id": "i1", "clientId": bus.clientUserMessageId,
                           "content": [{"type": "text", "text": "from the bus"}]}),
    ], "nextCursor": "older"}
    older = {"data": [
        codex_entry("t1", {"type": "agentMessage", "id": "i2", "text": "first answer"}),
    ], "nextCursor": None}
    codex = FakeCodex({None: newest, "older": older})
    server = CodexAppServer("thread", "unused")

    async def collect():
        buffer = ImportBuffer(ImportLimits())
        async for update in server.seen(codex):
            update.add_to(buffer)
        return buffer.snapshot(CodexHarness)

    snapshot = asyncio.run(collect())
    assert bodies(snapshot) == ["from the bus", "second answer"]
    assert snapshot.project == "/work/codex" and snapshot.source_id == "thread"
    assert [type(message) for message in snapshot.messages] == [ImportedRequest, ImportedReply]
    assert [call.method for call in codex.calls] == ["thread/resume", "thread/items/list"]


# OpenCode


def opencode_message(message_id, role, parts, **info):
    # As OpenCode saves them: only an assistant message records its completion.
    assistant = role == "assistant"
    return {"info": {"id": message_id, "sessionID": "ses_test", "role": role,
                     "time": {"created": 1, **({"completed": 2} if assistant else {})},
                     **({"parentID": "user"} if assistant else {}), **info},
            "parts": [{"id": f"{message_id}-{index}", "sessionID": "ses_test", "messageID": message_id, **part}
                      for index, part in enumerate(parts)]}


def opencode_export(path, project, revert=None):
    path.write_text(json.dumps({
        "info": {"id": "ses_test", "title": "Current task", "directory": str(project),
                 **({"revert": {"messageID": revert}} if revert else {})},
        "messages": [
            opencode_message("summary", "assistant", [{"type": "text", "text": "Keep model authority centralized."}],
                             summary=True),
            opencode_message("user", "user", [{"type": "text", "text": "Continue the refactor"},
                                              {"type": "file", "filename": "screenshot.png", "mime": "image/png"}]),
            opencode_message("assistant", "assistant", [
                {"type": "tool", "tool": "bash", "callID": "call", "state": {
                    "status": "completed", "input": {"command": "pytest"}, "output": "42 passed",
                    "time": {"start": 1, "end": 2}}},
                {"type": "text", "text": "Tests passed; the remaining work is UI routing."},
            ]),
            opencode_message("undone", "user", [{"type": "text", "text": "Reverted prompt"}]),
        ],
    }))
    return path


def test_opencode_snapshot_is_stopped_resumable_and_source_unchanged(tmp_path, comms):
    source = opencode_export(tmp_path / "export.json", tmp_path, revert="undone")
    before = source.read_bytes()
    directory = comms.root / "imported_sessions"
    acknowledgement = comms.threads.import_thread(
        OfflineHistory.of(OpenCodeHarness, source, AnySession()), name="imported")
    thread = comms.registry.require("imported")
    assert comms.registry.status(thread.name) == StoppedThreadStatus()
    assert acknowledgement.source_id == "ses_test" and acknowledgement.imported_messages == 5
    assert thread.title == GivenThreadText("Current task")
    assert source.read_bytes() == before
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(Path(thread.runtime.read_key).stat().st_mode) == 0o600
    records = [json.loads(line) for line in Path(thread.runtime.read_key).read_text().splitlines()]
    assert records[0]["type"] == "session" and records[0]["version"] == 3
    parent = None
    for record in records[1:]:
        assert record["parentId"] == parent
        parent = record["id"]
    context = "\n".join(event.text for event in comms.transcripts.thread_transcript_page(thread.name).events)
    assert "Keep model authority" in context and "42 passed" in context
    assert "remaining work" in context and "[Historical attachment: screenshot.png]" in context
    assert "Reverted prompt" not in context
    with pytest.raises(ValueError):
        comms.threads.import_thread(OfflineHistory.of(OpenCodeHarness, source, AnySession()), name="imported")


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and symlinks")
def test_import_refuses_redirected_session_directory(tmp_path, comms):
    source = opencode_export(tmp_path / "export.json", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (comms.root / "imported_sessions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        comms.threads.import_thread(OfflineHistory.of(OpenCodeHarness, source, AnySession()), name="imported")
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert not list(outside.iterdir())
    assert not comms.registry.snapshot().taken("imported")


def test_opencode_database_uses_readonly_snapshot(tmp_path):
    source = tmp_path / "opencode.db"
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE session(id TEXT, directory TEXT, title TEXT, revert TEXT);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INT, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, time_created INT, data TEXT);
        """)
        db.execute("INSERT INTO session VALUES(?,?,?,NULL)", ("session", str(tmp_path), "Title"))
        db.execute("INSERT INTO message VALUES(?,?,?,?)",
                   ("message", "session", 1, '{"role":"user","time":{"created":1},"summary":{"diffs":[]}}'))
        db.execute("INSERT INTO part VALUES(?,?,?,?)",
                   ("part", "message", 1, '{"type":"text","text":"Resume this task"}'))
    before = source.read_bytes()
    snapshot = read(OpenCodeHarness, source, session=NamedSession("session"))
    assert bodies(snapshot) == ["Resume this task"] and snapshot.title == GivenThreadText("Title")
    assert source.read_bytes() == before
    with pytest.raises(ValueError):
        read(OpenCodeHarness, source)


class FakeOpenCode:
    session = "ses_test"
    info = Session(id="ses_test", directory="/work/opencode", title="Live task")

    def __init__(self, messages):
        self.saved = [SavedMessage.model_validate(message) for message in messages]
        self.queries = []

    async def messages(self, query):
        self.queries.append(query)
        return self.saved

    async def status(self):
        from agent_comms.runtime_updates import TurnEnded

        return TurnEnded()


def test_opencode_server_reads_all_messages_including_the_users():
    from agent_comms.cli_bridges.opencode_protocol import OpenCodeStream

    connection = FakeOpenCode([
        opencode_message("user", "user", [{"type": "text", "text": "Live prompt"}]),
        opencode_message("assistant", "assistant", [{"type": "text", "text": "Live answer"}]),
    ])
    server = OpenCodeServer("ses_test", "http://unused", Path("/work/opencode"))

    async def collect():
        buffer = ImportBuffer(ImportLimits())
        async for update in server.seen(connection, OpenCodeStream(connection)):
            update.add_to(buffer)
        return buffer.snapshot(OpenCodeHarness)

    snapshot = asyncio.run(collect())
    assert bodies(snapshot) == ["Live prompt", "Live answer"]
    assert snapshot.title == GivenThreadText("Live task") and connection.queries == [{}]
    with pytest.raises(ValueError):
        OnlineHistory.of(OpenCodeHarness, "ses_test", DefaultAddress(), Path("/work"))


def test_opencode_server_leaves_out_what_the_user_reverted():
    from agent_comms.cli_bridges.opencode_protocol import OpenCodeStream

    connection = FakeOpenCode([
        opencode_message("user", "user", [{"type": "text", "text": "Kept prompt"}]),
        opencode_message("assistant", "assistant", [{"type": "text", "text": "Undone answer"}]),
    ])
    connection.info = Session(id="ses_test", directory="/work/opencode", revert={"messageID": "assistant"})

    async def collect():
        buffer = ImportBuffer(ImportLimits())
        async for update in OpenCodeServer("ses_test", "http://unused", Path("/work")).seen(
                connection, OpenCodeStream(connection)):
            update.add_to(buffer)
        return buffer.snapshot(OpenCodeHarness)

    assert bodies(asyncio.run(collect())) == ["Kept prompt"]


def test_import_cli_and_invalid_sources(tmp_path, capsys, comms):
    source = opencode_export(tmp_path / "export.json", tmp_path)
    assert main(["--root", str(comms.root), "import-thread", "--harness", "opencode",
                 "--source", str(source), "--name", "copied"]) == 0
    assert json.loads(capsys.readouterr().out)["thread"] == "copied"
    with pytest.raises(ValueError):
        read(OpenCodeHarness, source, session=NamedSession("wrong"))
    with pytest.raises(ValueError):
        read(OpenCodeHarness, tmp_path / "export.txt")
    with pytest.raises(ValueError):
        ImportLimits(messages=0)
