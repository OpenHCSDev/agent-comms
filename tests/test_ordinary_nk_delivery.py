"""Ordinary Comms sends on explicitly marked private roots use the canonical
publication protocol."""

from __future__ import annotations

import os

from agent_comms.messaging.channel_targets import ThreadTarget
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.messaging.messages import NoticeRole
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from conftest import installed_comms
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


def test_fresh_send_uses_canonical_publication_and_human_delivery_has_no_wake(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.registry.declare(
            NativeThread(name, frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    message = comms.messaging.send("sender", "beta", "ordinary send").message
    initial = comms.bus.bus.delivery(message.seq)
    assert initial.message == message
    assert initial.canonical_members == frozenset({"beta"})
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    reply = comms.messaging.send("sender", viewer, "human notification").message
    human = comms.bus.bus.delivery(reply.seq)
    assert isinstance(human.message.role, NoticeRole)
    assert human.recipients == ()
    assert human.decisions == ()
    assert comms.views.dm_display_page(ThreadTarget("sender"), worktree=Checkout(tmp_path)).messages[-1] == reply
