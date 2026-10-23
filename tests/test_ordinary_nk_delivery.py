"""Ordinary Comms sends on explicitly marked private roots use the canonical
publication protocol."""

from __future__ import annotations
from agent_comms.messages import NoticeRole

import os


from agent_comms.channel_targets import ThreadTarget
from agent_comms.child_process import ProcessIdentity
from conftest import installed_comms
from test_coordinated_runtime import tmp_path as private_root_fixture
from agent_comms.threads import Thread

tmp_path = private_root_fixture


def test_fresh_send_uses_canonical_publication_and_human_delivery_has_no_wake(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.registry.declare(
            Thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    message = comms.messaging.send("sender", "beta", "ordinary send").message
    initial = comms.bus.bus.delivery(message.seq)
    assert initial.message == message
    assert initial.audience.canonical_members == frozenset({"beta"})
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    reply = comms.messaging.send("sender", viewer, "human notification").message
    human = comms.bus.bus.delivery(reply.seq)
    assert isinstance(human.message.role, NoticeRole)
    assert human.audience.recipients == ()
    assert human.decisions == ()
    assert comms.views.dm_display_page(ThreadTarget("sender"), worktree=str(tmp_path)).messages[-1] == reply
