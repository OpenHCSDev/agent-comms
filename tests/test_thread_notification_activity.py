"""Activity invalidates only the open views containing the changed participants."""
from dataclasses import replace
import os

import pytest

from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.presentation import MessageNotification, WireRevision
from agent_comms.revision_views import NotificationsView
from agent_comms.private_path import FileRevision
from agent_comms.ui_model.observation import (
    NotificationsObserved, ObserveNotifications, ObserveThreads, ReadNotifications, ThreadPresented,
)
from conftest import publish_initial


def opened(tmp_path):
    root = tmp_path / 'wire'
    root.mkdir(mode=0o700)
    comms = Comms(root)
    names = frozenset(('sender', 'Alice', 'Bob', 'Carol', 'Dave'))
    for i, name in enumerate((*sorted(names), 'Recipient', 'Unrelated')):
        comms.registry.register(comms.registry.new_thread(
            name, frozenset(), str(tmp_path), created_at=17000.0 + i,
            process_identity=ProcessIdentity.capture(os.getpid())))
    comms.messaging.initialize_private_initial_protocol()
    for recipient in ('Alice', 'Bob', 'Carol', 'Dave', 'Recipient'):
        publish_initial(comms, 'sender', recipient, f'@{recipient} original message')
    publish_initial(comms, 'Bob', 'Alice', '@Alice independent incoming message')
    reading = ObserveThreads(names).observation()
    revision = comms.views.revision()
    list(reading.read(comms, revision))
    list(reading.read(comms, revision))
    assert all(isinstance(result, ThreadPresented) for result in reading.observed.results.values())
    return comms, reading, revision


