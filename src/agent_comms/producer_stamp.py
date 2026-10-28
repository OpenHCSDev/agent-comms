"""The complete bus writer family and its build-time projection commitments."""

import json
import sys

from agent_comms import package_stamp
from agent_comms.core_stamp import CoreStamp

if __name__ == "__main__":
    # The stamp factory's child names the commit it builds; it is bound before any module
    # below reads STAMP, so an archived source never asks Git for its identity.
    match sys.argv[1:]:
        case [str(commit)]:
            package_stamp.STAMP = CoreStamp(commit)
        case []:
            pass
        case unexpected:
            raise SystemExit(f"producer_stamp takes at most one commit, not {unexpected!r}")

from agent_comms.bus import Bus as Bus  # noqa: E402
from agent_comms.messages import BusRecord  # noqa: E402
from agent_comms.typed_table import ProducedTable, ProducedRow  # noqa: E402


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
