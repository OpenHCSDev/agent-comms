"""The complete bus writer family and its build-time projection commitments."""

import json

from agent_comms.bus import Bus as Bus
from agent_comms.messages import BusRecord
from agent_comms.typed_table import ProducedTable, ProducedRow


def complete_writer_family() -> tuple[type[ProducedRow], ...]:
    """Every record kind's declared outputs, read in full. Reading them finishes the families
    those declarations discover (the task outputs BusRecord derives), so a producer lineage
    read later sees every writer, whichever module a process imported first."""
    return tuple(row for record in BusRecord.members_with(BusRecord)
                 for owner in record.writer_declarations() for row in owner.writes)


complete_writer_family()


def stamped_digests() -> dict[str, str]:
    """The Bus import above completes the family before reading any producer registry."""
    return {
        row.declared_name: row.producer_digest()
        for row in ProducedTable.members_with(ProducedTable)
    }


if __name__ == "__main__":
    print(json.dumps(stamped_digests(), sort_keys=True))
