"""Retained source applicability follows the original frozen recipient birth."""
from dataclasses import replace

from agent_comms.comms import Comms
from test_private_bus_checkpoint import _root


def facts_for(comms, owner):
    with comms.bus.log.certified_read() as source:
        return tuple(source.retained_task_facts(owner.incarnation))


def test_ordinary_peer_reply_preserves_facts_but_new_user_instruction_changes_them(tmp_path):
    comms, _ = _root(tmp_path)
    owner = comms.registry.require("Alice")
    original = comms.messaging.send_user_message("#team", "Preserve this original constraint", worktree=str(tmp_path))
    before = facts_for(comms, owner)
    assert [fact.source.reference for fact in before] == [original.reference]
    comms.messaging.send_message("Bob", "#team", "Ordinary peer acknowledgement", notice=True)
    assert facts_for(comms, owner) == before
    changed = comms.messaging.send_user_message("#team", "A distinct new original instruction", worktree=str(tmp_path))
    after = facts_for(comms, owner)
    assert after != before
    assert [fact.source.reference for fact in after] == [original.reference, changed.reference]


def test_original_channel_recipient_reuse_and_new_member_do_not_inherit_old_facts(tmp_path):
    comms, _ = _root(tmp_path)
    original = comms.registry.require("Alice")
    first = comms.messaging.send_user_message("#team", "Original exact user constraint", worktree=str(tmp_path))
    assert [fact.source.reference for fact in facts_for(comms, original)] == [first.reference]

    outsider = replace(comms.registry.require("outsider"), tags=frozenset({"team"}))
    comms.registry.register(outsider)
    assert facts_for(comms, outsider) == ()
    comms.registry.remove("Alice")
    replacement = replace(original, created_at=17005.0)
    comms.registry.register(replacement)
    assert facts_for(comms, replacement) == ()

    second = comms.messaging.send_user_message("#team", "New exact user constraint", worktree=str(tmp_path))
    reopened = Comms(comms.root)
    assert [fact.source.reference for fact in facts_for(reopened, replacement)] == [second.reference]
    assert [fact.source.reference for fact in facts_for(reopened, outsider)] == [second.reference]
    assert [fact.source.reference for fact in facts_for(reopened, original)] == [first.reference]
