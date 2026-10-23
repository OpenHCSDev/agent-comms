"""Retained source applicability follows the original frozen recipient birth."""

from agent_comms.channel_targets import CommandTarget

from agent_comms.active_route import resolve_comms_route
from agent_comms.messages import NoticeRole
from dataclasses import replace

from agent_comms.comms import Comms
from agent_comms.retained_task_facts import RetainedTaskFacts
from bus_record_fixture import context_comms


def facts_for(comms, owner):
    return RetainedTaskFacts.capture(comms.bus.bus, owner.incarnation).facts


def test_ordinary_peer_reply_preserves_facts_but_new_user_instruction_changes_them(tmp_path):
    comms = context_comms(tmp_path)
    owner = comms.registry.require("Alice")
    original = comms.messaging.send_user_message(CommandTarget.named('#team'), "Preserve this original constraint", worktree=str(tmp_path))
    before = facts_for(comms, owner)
    assert [fact.source.reference for fact in before] == [original.reference]
    comms.messaging.send("Bob", "#team", "Ordinary peer acknowledgement", role=NoticeRole()).message
    assert facts_for(comms, owner) == before
    changed = comms.messaging.send_user_message(CommandTarget.named('#team'), "A distinct new original instruction", worktree=str(tmp_path))
    after = facts_for(comms, owner)
    assert after != before
    assert [fact.source.reference for fact in after] == [original.reference, changed.reference]


def test_original_channel_recipient_reuse_and_new_member_do_not_inherit_old_facts(tmp_path):
    comms = context_comms(tmp_path)
    original = comms.registry.require("Alice")
    first = comms.messaging.send_user_message(CommandTarget.named('#team'), "Original exact user constraint", worktree=str(tmp_path))
    assert [fact.source.reference for fact in facts_for(comms, original)] == [first.reference]

    outsider = replace(comms.registry.require("outsider"), tags=frozenset({"team"}))
    comms.registry.register(outsider)
    assert facts_for(comms, outsider) == ()
    comms.registry.remove("Alice")
    replacement = replace(original, created_at=17005.0)
    comms.registry.register(replacement)
    assert facts_for(comms, replacement) == ()

    second = comms.messaging.send_user_message(CommandTarget.named('#team'), "New exact user constraint", worktree=str(tmp_path))
    reopened = Comms(resolve_comms_route(comms.root))
    assert [fact.source.reference for fact in facts_for(reopened, replacement)] == [second.reference]
    assert [fact.source.reference for fact in facts_for(reopened, outsider)] == [second.reference]
    assert [fact.source.reference for fact in facts_for(reopened, original)] == [first.reference]
