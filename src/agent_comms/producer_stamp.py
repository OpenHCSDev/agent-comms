"""The complete bus writer family and its build-time projection commitments."""

import json

from agent_comms.bus import Bus as Bus
from agent_comms.typed_table import ProducedTable


def stamped_digests() -> dict[str, str]:
    """The Bus import above completes the family before reading any producer registry."""
    return {
        row.declared_name: row.producer_digest()
        for row in ProducedTable.members_with(ProducedTable)
    }


if __name__ == "__main__":
    print(json.dumps(stamped_digests(), sort_keys=True))
