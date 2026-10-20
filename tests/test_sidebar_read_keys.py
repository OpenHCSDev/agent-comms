"""The existing transcript reader consumes file changes, not every sidebar event."""
from unittest.mock import patch

from agent_comms.agent_backend import RuntimeLocation
from agent_comms.private_path import FileRevision
from agent_comms.registry_document import RosterVisibility
from agent_comms.wire_watch import NativeFileWatch
from native_unread_fixture import completed_snapshot
from test_thread_unread import append, setup_thread


def test_empty_native_watch_has_no_paths_or_native_descriptor():
    watcher = NativeFileWatch.open()
    try:
        assert watcher.changed_paths() == frozenset()
        assert not watcher.invalid
    finally:
        watcher.close()


