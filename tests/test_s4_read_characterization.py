"""Current S4 Part B: authority lifetime and disposable-index recovery."""

from agent_comms.repo import Checkout
from agent_comms.coordination_errors import DMScopeChanged
from agent_comms.read_basis import NamedViewer
from agent_comms.read_ledger import ReadLedger
from dataclasses import replace

import pytest

from conftest import installed_comms
from agent_comms.threads import Thread


def test_replaced_human_incarnation_rejects_painted_proof_and_prior_reads(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(Thread('worker', frozenset({'team'}), Checkout.from_wire(str(tmp_path))))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    message = comms.messaging.send("worker", "#team", "painted").message
    page = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_page_read(page, worktree=str(tmp_path))
    assert message.seq in comms.bus.reads.cursor(viewer, comms.registry.snapshot()).for_conversation(ReadLedger.conversation(message, comms.registry.snapshot()))
    original = comms.registry.require(viewer)
    comms.registry.unregister(viewer)
    comms.registry.remove(viewer)
    comms.registry.register(replace(original, created_at=original.created_at + 1))
    assert message.seq not in comms.bus.reads.cursor(viewer, comms.registry.snapshot()).for_conversation(ReadLedger.conversation(message, comms.registry.snapshot()))
    with pytest.raises(DMScopeChanged):
        comms.views.mark_page_read(page, worktree=str(tmp_path))
    refreshed = comms.views.channel_display_page("#team", viewer=NamedViewer(comms.messaging.user_identity(str(tmp_path)).name))
    comms.views.mark_page_read(refreshed, worktree=str(tmp_path))
    assert message.seq in comms.bus.reads.cursor(viewer, comms.registry.snapshot()).for_conversation(ReadLedger.conversation(message, comms.registry.snapshot()))


