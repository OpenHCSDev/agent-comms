"""The real SessionStart command works before a commander connects to the bus."""

import json
import os
import subprocess
import sys

import pytest
from pathlib import Path

from agent_comms.behaviour_patterns import PostmortemFailure
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.rule_file import RuleFile
from agent_comms.todos import Slop
from agent_comms_hooks.reread import CommanderReread, VoiceReminder


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
@pytest.mark.parametrize("configuration", ["configured", "missing", "invalid"])
def test_session_start_reads_all_packaged_files_without_a_bus(tmp_path, source, configuration):
    home = tmp_path / "home"
    home.mkdir()
    bus = tmp_path / "bus"
    bus.mkdir()
    path = CommanderConfig.path(home)
    if configuration != "missing":
        path.parent.mkdir(parents=True)
        if configuration == "invalid":
            path.write_text("{invalid JSON")
        else:
            config = CommanderConfig(
                commander="fresh-commander",
                root=bus,
                state=tmp_path / "state",
                repos=(),
                worktrees=tmp_path / "worktrees",
                scratch=tmp_path / "scratch",
                disk=tmp_path,
            )
            path.write_text(config.model_dump_json())
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    result = subprocess.run(
        [sys.executable, "-m", "agent_comms_hooks.skill"],
        input=json.dumps(
            {"hook_event_name": "SessionStart", "source": source, "session_id": "fresh-session",
             "transcript_path": str(transcript)}
        ),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(context) < 10_000
    parts = [Path(line) for line in context.splitlines()[1:]]
    assert parts == sorted(transcript.with_suffix(".reread").iterdir())
    text = "".join(part.read_text() for part in parts)
    expected_notice = (
        "Commander: fresh-commander."
        if configuration == "configured"
        else "No commander is configured"
    )
    assert text.startswith(expected_notice)
    catalog = Slop.catalog()
    readme = catalog / "README.md"
    assert text.split("\n\n", 1)[1] == "\n\n".join((
        CommanderConfig.packaged_voice().read_text(),
        CommanderConfig.packaged_indictment().read_text(),
        CommanderConfig.packaged_spine().read_text(),
        "# Recorded failures\n\n" + PostmortemFailure.catalog(),
        RuleFile.document(CommanderConfig.packaged_skill())[1],
        *(rule.render() for rule in RuleFile.packaged()),
        readme.read_text(),
        *(path.read_text() for path in sorted(set(catalog.glob("*.md")) - {readme})),
    ))
    assert not tuple(bus.iterdir())


def usage(tokens):
    return json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": tokens, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
        separators=(",", ":"))


def hook_record(transcript):
    return {"hook_event_name": "PostToolUse", "session_id": "s", "transcript_path": str(transcript)}


@pytest.mark.parametrize("member", [CommanderReread, VoiceReminder], ids=lambda member: member.__name__)
def test_each_reminder_keeps_its_own_cadence_and_resets_at_compaction(tmp_path, member):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    hook = member.decode(hook_record(transcript))
    assert hook.take(250_000) == ()  # not a commander session: no parts were written
    hook.parts.mkdir()
    (hook.parts / "01.md").write_text("sources")
    hook.voice.write_text("voice")
    assert hook.take(250_000)
    assert hook.take(250_000 + member.every - 1) == ()
    assert hook.take(250_000 + member.every)
    assert hook.take(20_000)  # compaction shrank the context below the mark


def run_hook(program, transcript, event, monkeypatch, capsys, *lines):
    import io

    transcript.write_text("".join(line + "\n" for line in lines))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"hook_event_name": event, "session_id": "s", "transcript_path": str(transcript)})))
    program.main()
    return capsys.readouterr().out


def test_every_due_reminder_goes_out_in_one_hook_output_with_the_voice_inline(
        tmp_path, monkeypatch, capsys):
    from agent_comms_hooks import reread, skill

    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert not run_hook(reread, transcript, "PostToolUse", monkeypatch, capsys, usage(250_000))
    assert run_hook(skill, transcript, "SessionStart", monkeypatch, capsys, usage(250_000))
    first = run_hook(reread, transcript, "PostToolUse", monkeypatch, capsys, usage(250_001))
    (document,) = first.splitlines()
    assert json.loads(document)["hookSpecificOutput"]["additionalContext"] == CommanderConfig.packaged_voice().read_text()
    both = run_hook(reread, transcript, "PostToolUse", monkeypatch, capsys, usage(350_000))
    (document,) = both.splitlines()
    context = json.loads(document)["hookSpecificOutput"]["additionalContext"]
    assert CommanderConfig.packaged_voice().read_text() in context
    assert all(str(part) in context for part in sorted(transcript.with_suffix(".reread").iterdir()))


@pytest.mark.parametrize("path", CommanderConfig.packaged_rules_files(), ids=lambda path: path.name)
def test_packaged_rule_has_a_reason(path):
    assert RuleFile.read(path).why


def test_packaged_descriptions_do_not_repeat_rule_kind():
    for path in CommanderConfig.packaged_rules_files():
        metadata, _ = RuleFile.document(path)
        assert not metadata["description"].startswith("HARD RULE:"), path.name


def test_failure_catalog_numbers_every_evidenced_member_once():
    members = sorted(PostmortemFailure.members_with(PostmortemFailure), key=lambda member: member.number)
    assert [member.number for member in members] == list(range(1, len(members) + 1))
    assert all(member.evidence for member in members)


