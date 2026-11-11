"""A Core-launched session carries the whole deal in its system prompt, and a deal delivered
there owes the witness, not the reads: the audit and "did you mean it?" still come first."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_comms.cli_bridges import claude_launch
from agent_comms.installer.deal_prompt import DealPrompt
from agent_comms.installer.native_package import OWNER_INSTRUCTIONS
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.team.prompt_reads import (
    DealtRead,
    PromptReads,
)
from agent_comms.team.rule_deal import (
    AuditDue,
    DealtRuleGate,
    OpenedReads,
    SessionRuleDeal,
    StartedDeal,
    SystemPromptDelivery,
    Unaudited,
    Unread,
)
from agent_comms_hooks import LaunchedPrompt
from agent_comms_hooks.skill import DeckReader, DeckStart


@dataclass(frozen=True)
class PromptedDeal(SessionRuleDeal, DealtRuleGate):
    prompt: Path

    def delivery(self) -> SystemPromptDelivery:
        return SystemPromptDelivery(self.prompt)

    def refuse(self, reason: str) -> None:
        raise PermissionError(reason)


def test_prompt_carries_the_owner_instructions_then_every_dealt_file_once(tmp_path):
    prompt = DealPrompt.prepare(tmp_path / "SYSTEM_PROMPT.md")
    text = prompt.read_text(encoding="utf-8")
    assert text.startswith(OWNER_INSTRUCTIONS.read_text(encoding="utf-8").strip())
    for path in DeckReader.sources():
        assert text.count(path.read_text(encoding="utf-8").strip()) == 1, path
    reads = SystemPromptDelivery(prompt).reads()
    assert [read.source.path for read in reads] == [path.resolve() for path in DeckReader.sources()]
    assert all(read.current() for read in reads)


def test_a_deal_in_the_system_prompt_owes_the_audit_not_the_reads(tmp_path):
    deal = PromptedDeal(tmp_path / "deal", DealPrompt.prepare(tmp_path / "SYSTEM_PROMPT.md"))
    deal.deal(DeckReader, AuditDue(()), StartedDeal())
    record = deal.current()
    assert record.read_by(deal) == record.paths
    assert not tuple(Unread.owed_by(deal, record, OpenedReads()))
    assert tuple(Unaudited.owed_by(deal, record, OpenedReads()))


def test_a_file_changed_after_the_prompt_was_prepared_is_read_like_any_other(tmp_path):
    prompt = DealPrompt.prepare(tmp_path / "SYSTEM_PROMPT.md")
    delivery = SystemPromptDelivery(prompt)
    first, *rest = delivery.reads()
    delivery.manifest.write_text(json.dumps(FieldCodec.encode(
        PromptReads((DealtRead(first.source, "0" * 64), *rest)))))
    deal = PromptedDeal(tmp_path / "deal", prompt)
    deal.deal(DeckReader, AuditDue(()), StartedDeal())
    owed = tuple(Unread.owed_by(deal, deal.current(), OpenedReads()))
    assert [case.remaining for case in owed] == [(first.source.path,)]


def test_a_session_the_launcher_started_is_told_its_prompt_holds_the_deal(monkeypatch, tmp_path):
    prompt = DealPrompt.prepare(tmp_path / "SYSTEM_PROMPT.md")
    record = {"session_id": "s", "hook_event_name": "SessionStart",
              "transcript_path": str(tmp_path / "s.jsonl")}
    monkeypatch.delenv(LaunchedPrompt.variable, raising=False)
    assert DeckStart.decode(LaunchedPrompt.carried(record)).delivery() != SystemPromptDelivery(prompt)
    monkeypatch.setenv(LaunchedPrompt.variable, str(prompt))
    delivery = DeckStart.decode(LaunchedPrompt.carried(record)).delivery()
    assert delivery == SystemPromptDelivery(prompt)


def test_the_claude_launcher_appends_the_prompt_and_never_execs_itself(monkeypatch, tmp_path):
    own, real = tmp_path / "own", tmp_path / "real"
    for directory in (own, real):
        directory.mkdir()
        (directory / "claude").write_text("#!/bin/sh\n")
        (directory / "claude").chmod(0o755)
    prompt = DealPrompt.prepare(tmp_path / "SYSTEM_PROMPT.md")
    monkeypatch.setattr(claude_launch, "SYSTEM_PROMPT", prompt)
    monkeypatch.setattr(claude_launch.sys, "argv", [str(own / "claude"), "--resume", "abc"])
    monkeypatch.setenv("PATH", f"{own}:{real}")
    launched = {}
    monkeypatch.setattr(claude_launch.os, "execve",
                        lambda path, argv, env: launched.update(path=path, argv=argv, env=env))
    claude_launch.main()
    assert launched["path"] == real / "claude"
    assert launched["argv"][1:] == ["--append-system-prompt-file", str(prompt), "--resume", "abc"]
    assert launched["env"][LaunchedPrompt.variable] == str(prompt)


def test_a_later_install_never_rewrites_the_deal_a_running_session_launched_with(monkeypatch, tmp_path):
    link = tmp_path / "SYSTEM_PROMPT.md"
    first = DealPrompt.prepare(link)
    held = SystemPromptDelivery(first).manifest.read_bytes()
    assert link.resolve() == first
    rendered = DealPrompt.render()
    monkeypatch.setattr(DealPrompt, "render", classmethod(
        lambda cls: DealPrompt(rendered.text + "\nA rule changed.\n", PromptReads(rendered.reads.reads[1:]))))
    second = DealPrompt.prepare(link)
    assert second != first and link.resolve() == second
    assert first.read_text(encoding="utf-8") == rendered.text
    assert SystemPromptDelivery(first).manifest.read_bytes() == held
    assert SystemPromptDelivery(link).reads() == rendered.reads.reads[1:]
