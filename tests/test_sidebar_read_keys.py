"""The transcript reader's native registration owns only its declared sources."""

from agent_comms.bus import Bus


def test_empty_native_registration_has_no_changed_paths():
    with Bus.register_sources() as watcher:
        assert watcher.changed_paths() == frozenset()
