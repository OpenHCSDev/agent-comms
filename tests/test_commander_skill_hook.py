"""The real SessionStart command works before a commander connects to the bus."""

import json
import os
import subprocess
import sys

import pytest
from pathlib import Path

from agent_comms.behaviour_patterns import PostmortemFailure
from agent_comms.commander.config import CommanderConfig
from agent_comms.commander.rule_file import HardRuleFile, RuleFile
from agent_comms.todos import Slop


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
                board=tmp_path / "board.md",
                narrative=tmp_path / "narrative.md",
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
        CommanderConfig.packaged_indictment().read_text(),
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


def test_reread_is_due_every_hundred_thousand_context_tokens(tmp_path, monkeypatch, capsys):
    import io
    from agent_comms_hooks import reread, skill

    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def hook(program, event, *lines):
        transcript.write_text("".join(line + "\n" for line in lines))
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"hook_event_name": event, "session_id": "s", "transcript_path": str(transcript)})))
        program.main()
        return capsys.readouterr().out

    assert not hook(reread, "PostToolUse", usage(250_000))
    assert hook(skill, "SessionStart", usage(250_000))
    assert not hook(reread, "PostToolUse", usage(250_000), usage(349_999))
    assert hook(reread, "PostToolUse", usage(350_000))
    assert hook(skill, "SessionStart", usage(20_000))
    assert not hook(reread, "PostToolUse", usage(119_999))
    assert hook(reread, "PostToolUse", json.dumps({"type": "user", "message": {}}), usage(120_000))


@pytest.mark.parametrize("path", CommanderConfig.packaged_rules_files(), ids=lambda path: path.name)
def test_packaged_rule_has_a_reason(path):
    assert RuleFile.read(path).why


def test_packaged_descriptions_do_not_repeat_rule_kind():
    for path in CommanderConfig.packaged_rules_files():
        metadata, _ = RuleFile.document(path)
        assert not metadata["description"].startswith("HARD RULE:"), path.name


def test_failure_catalog_has_fifteen_evidenced_members():
    members = sorted(PostmortemFailure.members_with(PostmortemFailure), key=lambda member: member.number)
    assert [member.number for member in members] == list(range(1, 16))
    assert PostmortemFailure.catalog().splitlines() == [
        f"{member.number}. {member.title}: {member.evidence}" for member in members
    ]
    assert all(member.evidence for member in members)


def test_rule_owner_orders_types_and_hook_context(tmp_path, monkeypatch, capsys):
    import io
    from agent_comms_hooks.skill import main

    paths = []
    for name, kind in (("ordinary", "feedback"), ("required", "hard-rule"), ("paper", "reference")):
        path = tmp_path / f"{name}.md"
        path.write_text(
            f"---\nname: {name}\ndescription: the {name} rule\ntype: {kind}\n---\n"
            f"Body for {name}.\n\n**Why:** reason for {name}\n"
        )
        paths.append(path)
    monkeypatch.setattr(CommanderConfig, "packaged_rules_files", classmethod(lambda cls: tuple(paths)))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "fixture", "transcript_path": str(transcript)})))
    rules = RuleFile.packaged()
    assert isinstance(rules[0], HardRuleFile)
    assert [rule.name for rule in rules] == ["required", "ordinary", "paper"]
    assert [rule.name for rule in rules if rule.review_rules()] == ["required", "ordinary"]
    main()
    context = "".join(Path(line).read_text() for line in json.loads(
        capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"].splitlines()[1:])
    indictment = CommanderConfig.packaged_indictment().read_text()
    assert context.split("\n\n", 1)[1].startswith(indictment)
    assert context.index(PostmortemFailure.catalog()) < context.index("# Comms commander")
    assert context.index("# Comms commander") < context.index("required: the required rule")
    assert context.index("required: the required rule") < context.index("ordinary: the ordinary rule")
    assert "\nname:" not in context and "\ntype:" not in context and "\ndescription:" not in context
    assert context.count("reason for required") == 1


def test_hook_and_review_refuse_the_same_missing_reason(tmp_path, monkeypatch):
    import io
    from agent_comms.commander.rules import rules
    from agent_comms_hooks.skill import main

    path = tmp_path / "unreasoned.md"
    path.write_text("---\nname: unreasoned\ndescription: do this\ntype: feedback\n---\nBody only.\n")
    monkeypatch.setattr(CommanderConfig, "packaged_rules_files", classmethod(lambda cls: (path,)))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "fixture", "transcript_path": str(transcript)})))
    with pytest.raises(ValueError, match="unreasoned.md: missing Why"):
        main()
    with pytest.raises(ValueError, match="unreasoned.md: missing Why"):
        rules()
