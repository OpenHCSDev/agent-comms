"""One-time human-read binding change, retired after P419 installation.

The ledger's current codec owns every cursor and notice. No runtime reader
accepts the discarded JSONL binding after this tool has run.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_comms.bus import Bus
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried
from agent_comms.read_ledger import ReadLedger
from agent_comms.recorded_bus import RecordedBusIdentity


def rebind_read_identity(root: Path, original: RecordedBusIdentity) -> Carried:
    ledger = ReadLedger.at(root)
    if not ledger.path.exists():
        return Carried(0)
    destination = Bus.path_in(root)
    if not destination.is_file():
        raise RelationViolationError("Read binding needs the verified SQLite bus")
    converted = RecordedBusIdentity.capture(destination)
    with ledger.locked():
        document = ledger._read_unlocked()
        if not document.bus_identity.matches(original):
            return Carried(0)
        rebound = replace(document, bus_identity=converted)
        if rebound == document:
            return Carried(0)
        ledger._publish_unlocked(rebound)
    return Carried(1, rewritten=(ledger.filename,))
