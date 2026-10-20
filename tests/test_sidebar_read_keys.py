"""The existing transcript reader consumes file changes, not every sidebar event."""

from agent_comms.wire_watch import NativeFileWatch


def test_empty_native_watch_has_no_paths_or_native_descriptor():
    watcher = NativeFileWatch.open()
    try:
        assert watcher.changed_paths() == frozenset()
        assert not watcher.invalid
    finally:
        watcher.close()


