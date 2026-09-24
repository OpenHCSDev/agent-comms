"""Direct input state survives a process exit without replaying an attempt."""

from pathlib import Path

import pytest

from agent_comms.input_disposition import AcpDeliveryCursors, InputDispositions


def test_unknown_is_durable_and_native_start_is_a_cas(tmp_path: Path) -> None:
    store = InputDispositions(tmp_path)
    assert store.record("bus:7", seq=7, owner="kid", admission=3, target="kid", text="ask")
    assert not store.record("bus:7", seq=7, owner="kid", admission=3, target="kid", text="ask")
    assert InputDispositions(tmp_path).unknown(frozenset({"kid"}))[0]["sequence"] == 7

    native_id = "a" * 32
    assert store.bind("bus:7", admission=3, turn_id="turn-1", native_id=native_id, text="full ask")
    assert not store.started("bus:7", turn_id="turn-1", native_id="b" * 32, text="full ask")
    assert not store.started("bus:7", turn_id="turn-1", native_id=native_id, text="wrong")
    assert store.started("bus:7", turn_id="turn-1", native_id=native_id, text="full ask")
    assert not store.started("bus:7", turn_id="turn-1", native_id=native_id, text="full ask")
    assert InputDispositions(tmp_path).unknown(frozenset({"kid"})) == []


def test_cursor_is_durable_and_independent_of_ui_read_marker(tmp_path: Path) -> None:
    cursor = AcpDeliveryCursors(tmp_path)
    assert cursor.initialize(frozenset({"kid"}), "kid", high_water=4, fresh=True) == (4, 0)
    cursor.advance(frozenset({"kid"}), 7)
    assert AcpDeliveryCursors(tmp_path).initialize(
        frozenset({"kid", "old-kid"}), "kid", high_water=9, fresh=False
    ) == (7, 0)


def test_legacy_cursor_does_not_authorize_old_inputs(tmp_path: Path) -> None:
    cursor = AcpDeliveryCursors(tmp_path)
    assert cursor.initialize(frozenset({"kid"}), "kid", high_water=9, fresh=False) == (0, 9)
    with pytest.raises(ValueError):
        cursor.advance(frozenset({"kid"}), -1)
