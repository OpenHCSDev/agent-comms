"""Todo rows gain Core's wording corrections on the holder's published messages (P104).

No saved strike, order or assignment is changed. Production readers accept
only the new declared schema.
"""

from __future__ import annotations

from agent_comms.installer import carry


class TodoCorrections(carry.TodoColumnCarry):
    """Todo rows gain the empty corrections record; every stored strike stays as it was."""

    column = "corrections"
