"""A Claude fork's stall is read from its own transcript; a slow install phase from its run's
result record. Neither row is read from prose."""

import json
from pathlib import Path

import pytest

from transcript_record_fixture import transcript_record

from agent_comms.build_identity import RecordedBuildIdentity, RepositoryCommit
from agent_comms.cli_bridges.claude_protocol import (
    AnsweredSubagent,
    CallingSubagent,
    PromptedSubagent,
)
from agent_comms.cli_bridges.claude_transcript import ClaudeTranscript
from agent_comms.commander.standing import ForkStalled, SlowInstallPhase
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.interpreter_operation import (
    PhaseTiming,
    StopServicesPhase,
    TreeClosePhase,
)
from agent_comms.installer.launch import InstallRun
from agent_comms.installer.logs import InstallLogs
from agent_comms.installer.request import InstallRequest
from agent_comms.installer.runtime_resolution import CorePackage
from agent_comms.installer.upgrade import FreshRehearsal, RehearsedUpgrade
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms_hooks import SubagentId

PROMPT = {"type": "user", "uuid": str(transcript_record("prompt")), "promptId": "p",
          "timestamp": "2026-10-30T10:00:00Z", "message": {"role": "user", "content": "find it"}}
CALL = {"type": "assistant", "uuid": str(transcript_record("call")),
        "timestamp": "2026-10-30T10:01:00Z",
        "message": {"content": [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": "Waiting for the run."},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "timeout 900 sleep 900", "description": "wait"}}]}}
RESULT = {"type": "user", "uuid": str(transcript_record("result")), "promptId": "p",
          "timestamp": "2026-10-30T10:16:00Z",
          "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "done"}]}}
ANSWER = {"type": "assistant", "uuid": str(transcript_record("answer")),
          "timestamp": "2026-10-30T10:17:00Z",
          "message": {"content": [{"type": "text", "text": "Found it."}]}}
INTERRUPTED = {"type": "user", "uuid": str(transcript_record("stop")), "promptId": "p",
               "timestamp": "2026-10-30T10:02:00Z",
               "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}
NOTE = {"type": "attachment", "uuid": str(transcript_record("note")),
        "timestamp": "2026-10-30T10:03:00Z", "attachment": {"type": "foreign"}}


def transcript(tmp_path: Path, *records: dict, partial: str = "") -> ClaudeTranscript:
    path = tmp_path / "agent-a24a52dcade3de78d.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records) + partial)
    return ClaudeTranscript(path)


def test_a_fork_whose_newest_deciding_record_is_a_call_owes_its_next_step(tmp_path):
    progress = transcript(tmp_path, PROMPT, CALL, partial='{"type": "assist').progress()
    assert isinstance(progress, CallingSubagent)
    owed, = progress.owing()
    assert owed.call.arguments["command"] == "timeout 900 sleep 900"
    assert "timeout 900 sleep 900" in owed.doing(owed.at + 27 * 60)
    assert "27 min ago" in owed.doing(owed.at + 27 * 60)


def test_a_tool_result_still_owes_the_call_that_produced_it(tmp_path):
    assert isinstance(transcript(tmp_path, PROMPT, CALL, RESULT).progress(), CallingSubagent)


def test_a_prompted_fork_with_no_call_owes_its_first_one(tmp_path):
    progress = transcript(tmp_path, PROMPT).progress()
    assert progress == PromptedSubagent(progress.at)
    assert progress.owing() == (progress,)


@pytest.mark.parametrize("records", ((PROMPT, CALL, RESULT, ANSWER), (PROMPT, CALL, INTERRUPTED, NOTE)))
def test_an_answered_or_interrupted_fork_owes_nothing(tmp_path, records):
    progress = transcript(tmp_path, *records).progress()
    assert progress == AnsweredSubagent() and progress.owing() == ()


def test_the_subagent_id_is_named_by_its_transcript():
    assert SubagentId.of_transcript(Path("x/agent-a24a52dcade3de78d.jsonl")).fork == "claude-a24a52dcade3de78d"


def test_a_stalled_fork_row_names_the_fork_its_quiet_minutes_and_its_last_call(tmp_path):
    owed = transcript(tmp_path, PROMPT, CALL).progress()
    fork = RecordedThreadIncarnation("claude-a24a52dcade3de78d", 1792869186.0)
    row = ForkStalled(fork, owed.at, "claude-code", owed.at, owed)
    line = row.line()
    assert line.startswith("STALLED fork claude-a24a52dcade3de78d of claude-code")
    assert "timeout 900 sleep 900" in line and "read its tool calls and send its next step" in line
    assert row.began() == owed.at + ForkStalled.seconds
    later = ForkStalled(fork, owed.at, "claude-code", owed.at + 60, owed)
    assert row.observation_key() == later.observation_key()


TARGET = "238db6176bafb56e3d28297c68eb94a4a33a6b3c"


def finished_install(logs: InstallLogs, tmp_path: Path, target: str, phases: list[PhaseTiming]) -> None:
    unit = logs.minted()
    run = InstallRun(InstallRequest(checkout=tmp_path, to=target, project=tmp_path),
                     logs.root, tmp_path, target)
    unit.request.write_text(json.dumps(FieldCodec.encode(run)))
    upgrade = RehearsedUpgrade("a" * 40, target, logs.root, (), FreshRehearsal(), phases=phases)
    unit.result.write_text(json.dumps(FieldCodec.encode(upgrade)))


def test_the_installed_runs_phases_over_the_healthy_line_are_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(RecordedBuildIdentity, "installed", classmethod(
        lambda cls: cls((RepositoryCommit(CorePackage.repository, TARGET),))))
    logs = InstallLogs(tmp_path / ".bus")
    finished_install(logs, tmp_path, "c" * 40, [PhaseTiming(TreeClosePhase(), 400.0, 400.0)])
    finished_install(logs, tmp_path, TARGET, [PhaseTiming(StopServicesPhase(), 234.0, 234.0),
                                              PhaseTiming(TreeClosePhase(), 59.0, 59.0)])
    comms = type("Comms", (), {"root": logs.root})()
    row, = SlowInstallPhase.violations(comms, "claude-code")
    assert row.phase == StopServicesPhase() and row.seconds == 234.0
    assert row.line().startswith("SLOW install phase StopServicesPhase took 234 s")
    assert row.line().endswith("-> order the fix to that phase's owner")
