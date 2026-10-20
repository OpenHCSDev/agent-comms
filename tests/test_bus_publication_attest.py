"""A committed bus row is attested once, where the private bus row store indexes it."""

import json

import pytest

from agent_comms.bus_publication import PRIVATE_WIRE_FIELD, CommittedDelivery
from agent_comms.field_codec import MalformedRecord
from agent_comms.private_bus_checkpoint import DeliverySources
from conftest import publish_initial
from test_private_bus_checkpoint import _root


def _read_bus(comms):
    """Every committed delivery, through the row store's own pointers."""
    with comms.bus.log.checked_read() as source:
        captured = source.capture_sources(tuple(DeliverySources.select(source.connection)))
    return list(captured)


def test_a_tampered_row_is_refused_on_its_first_read(tmp_path):
    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "#team", "first")
    row = json.loads(comms.bus.log.path.read_bytes().splitlines()[-1])
    row[PRIVATE_WIRE_FIELD]["initial"]["decisions_digest"] = "0" * 64
    with comms.bus.log.locked(), pytest.raises(MalformedRecord, match="decisions do not match"):
        comms.bus.log.append_private_unlocked(comms.bus.log.read_metadata_unlocked(required=True), row)
    assert [d.message.body for d in _read_bus(comms)] == ["first"]
