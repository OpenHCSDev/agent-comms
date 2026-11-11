"""A replaced human incarnation loses its prior reads and its painted pages."""

from dataclasses import replace

import pytest

from agent_comms.coordination.errors import DMScopeChanged
from agent_comms.messaging.read_ledger import ReadLedger
from agent_comms.projects.checkouts import Checkout
from agent_comms.messaging.read_basis import NamedViewer
from agent_comms.registry.threads import NativeThread
from conftest import installed_comms


def test_replaced_human_incarnation_rejects_painted_proof_and_prior_reads(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(NativeThread('worker', frozenset({'team'}), Checkout(tmp_path)))
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    message = comms.messaging.send("worker", "#team", "painted").message

    def read() -> bool:
        snapshot = comms.registry.snapshot()
        return message.seq in comms.bus.reads.cursor(viewer, snapshot).for_conversation(
            ReadLedger.conversation(message, snapshot))

    def page():
        return comms.views.channel_display_page("#team", viewer=NamedViewer(viewer))

    painted = page()
    comms.views.mark_page_read(painted, worktree=Checkout(tmp_path))
    assert read()
    original = comms.registry.require(viewer)
    comms.registry.unregister(viewer)
    comms.registry.remove(viewer)
    comms.registry.register(replace(original, created_at=original.created_at + 1))
    assert not read()
    with pytest.raises(DMScopeChanged):
        comms.views.mark_page_read(painted, worktree=Checkout(tmp_path))
    comms.views.mark_page_read(page(), worktree=Checkout(tmp_path))
    assert read()
