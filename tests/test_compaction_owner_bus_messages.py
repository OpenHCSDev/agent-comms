"""Compaction and /prime re-deals inject Tristan's last verbatim bus messages for the bound thread."""

from __future__ import annotations

from pathlib import Path

from agent_comms.cli_bridges.antigravity_rule_gate import AntigravityRuleGate
from agent_comms.cli_bridges.hook_reread import SliceReminder
from agent_comms.cli_bridges.shared import Binding, BridgeBeginning
from agent_comms.comms import Comms
from agent_comms.messaging.channel_targets import ChannelTarget, ThreadTarget
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import ExternalThread
from agent_comms_hooks.skill import DeckStart, LightPrime, SessionSource


def _seed_bus_conversation(comms: Comms, tmp_path: Path) -> None:
    comms.channels.create_tag("comms")
    comms.channels.create_tag("other")
    worktree = Checkout(tmp_path)
    comms.registry.declare(
        ExternalThread("worker", frozenset({"comms"}), worktree, created_at=100.0)
    )
    comms.registry.declare(
        ExternalThread("peer", frozenset({"comms", "other"}), worktree, created_at=101.0)
    )
    comms.messaging.send_user_message(ChannelTarget("#comms"), "First channel directive", worktree=worktree)
    comms.messaging.send("peer", "#comms", "Agent chatter should be excluded")
    comms.messaging.send_user_message(ChannelTarget("#other"), "Unsubscribed channel directive", worktree=worktree)
    comms.messaging.send_user_message(ThreadTarget("worker"), "Direct DM directive for worker", worktree=worktree)
    comms.messaging.send_user_message(ChannelTarget("#comms"), "Latest channel directive", worktree=worktree)


def test_recent_owner_messages_filters_to_owner_dms_and_subscribed_channels(comms: Comms, tmp_path: Path) -> None:
    _seed_bus_conversation(comms, tmp_path)
    snapshot = comms.registry.snapshot()
    worker = snapshot.threads["worker"]

    messages = comms.bus.recent_owner_messages(worker, snapshot, limit=5)
    assert [m.body for m in messages] == [
        "First channel directive",
        "Direct DM directive for worker",
        "Latest channel directive",
    ]
    limited = comms.bus.recent_owner_messages(worker, snapshot, limit=2)
    assert [m.body for m in limited] == [
        "Direct DM directive for worker",
        "Latest channel directive",
    ]

    formatted = comms.bus.owner_bus_words(worker, snapshot, limit=5)
    assert "WHAT TRISTAN ACTUALLY SAID LAST ON THE BUS" in formatted
    assert "DM -> worker" in formatted
    assert "#comms" in formatted
    assert "Direct DM directive for worker" in formatted
    assert "Agent chatter" not in formatted
    assert "Unsubscribed channel" not in formatted


def test_deck_start_take_and_prime_inject_owner_bus_messages_while_slice_reminder_skips(
    comms: Comms, tmp_path: Path,
) -> None:
    _seed_bus_conversation(comms, tmp_path)
    snapshot = comms.registry.snapshot()
    worker = snapshot.threads["worker"]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    binding = Binding(
        root=str(comms.root),
        thread="sess-1",
        start=BridgeBeginning(),
        owner=worker.incarnation,
    )

    start = DeckStart(
        session_id="sess-1",
        transcript_path=str(transcript),
        hook_event_name="SessionStart",
        source=SessionSource("startup"),
    )
    taken = start.take(comms, binding)
    assert any("Direct DM directive for worker" in part for part in taken)
    assert any("Latest channel directive" in part for part in taken)

    primed = start.primed(LightPrime, comms, binding)
    assert any("Direct DM directive for worker" in part for part in primed)

    reminder = SliceReminder(
        session_id="sess-1",
        transcript_path=str(transcript),
        hook_event_name="PostToolUse",
    )
    assert reminder.owner_bus_recall(comms, snapshot, "worker") == ()


def test_antigravity_rule_gate_includes_owner_bus_words_in_order_text(
    comms: Comms, tmp_path: Path, monkeypatch,
) -> None:
    _seed_bus_conversation(comms, tmp_path)
    snapshot = comms.registry.snapshot()
    worker = snapshot.threads["worker"]
    bus_words = comms.bus.owner_bus_words(worker, snapshot)

    gate = AntigravityRuleGate(
        transcript_path=tmp_path / "transcript.jsonl",
        artifact_dir=tmp_path,
        cwd=tmp_path,
        workspace_paths=(tmp_path,),
    )
    monkeypatch.setattr(gate, "owner_bus_words", lambda: bus_words)

    sections = gate._owner_prompt_sections(("Local launcher prompt",))
    assert len(sections) == 2
    assert "Local launcher prompt" in sections[0]
    assert "Direct DM directive for worker" in sections[1]
    assert "Latest channel directive" in sections[1]
