from __future__ import annotations

from agent_comms.coordination.errors import PublicationActivationBlocked
from agent_comms.coordination.private_runtime_schema import PrivateCoordinatorTable
from agent_comms.storage.typed_table import ImmutableRows


class ResponseTable(ImmutableRows, PrivateCoordinatorTable):
    """An immutable response definition with its write checks and append evidence."""

    schema_error = PublicationActivationBlocked
