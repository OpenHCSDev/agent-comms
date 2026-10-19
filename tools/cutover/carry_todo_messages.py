"""Todo rows gain the bus messages each order was sent in (P116).

No saved strike, order or assignment is changed. Production readers accept
only the new declared schema.
"""

from __future__ import annotations

from . import carry


class TodoMessages(carry.TodoColumnCarry):
    """Todo rows gain the empty order-message record; every stored verdict stays as it was."""

    column = "messages"


if __name__ == "__main__":
    TodoMessages.main()
