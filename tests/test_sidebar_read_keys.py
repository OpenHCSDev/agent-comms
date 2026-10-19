"""The existing transcript reader consumes file changes, not every sidebar event."""
from unittest.mock import patch

from agent_comms.agent_backend import RuntimeLocation
from native_unread_fixture import completed_snapshot
from test_thread_unread import append, setup_thread


def test_idle_sidebar_does_not_read_keys_and_append_only_reads_changed_thread(tmp_path):
    comms, source = setup_thread(tmp_path)
    other = tmp_path / "other.jsonl"
    append(source)
    append(other)
    comms.registry.declare(comms.registry.new_thread(
        'other', frozenset(), str(tmp_path), session_file=str(other)))
    original = RuntimeLocation.read_keys
    calls = []

    def observed(runtime):
        calls.append(runtime.path)
        return original(runtime)

    with patch.object(RuntimeLocation, 'read_keys', observed):
        completed_snapshot(comms, str(tmp_path))
        calls.clear()
        for _ in range(10):
            comms.views.viewer_snapshot(str(tmp_path))
        assert calls == []
        append(source, content="Only the worker changed")
        comms.views.viewer_snapshot(str(tmp_path))
        assert calls == [str(source)]
        completed_snapshot(comms, str(tmp_path))
        assert calls == [str(source)]
    comms.views.transcript_reads.close()
