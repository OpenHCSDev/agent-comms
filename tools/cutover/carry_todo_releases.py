"""Todo rows gain the releases a commander takes on them (P60).

No saved strike, order or assignment is changed. Production readers accept
only the new declared schema.
"""

from __future__ import annotations

from cutover import carry


class TodoReleases(carry.TodoColumnCarry):
    """Todo rows gain the empty released record; every stored assignment stays as it was."""

    after = "carry_order_run"
    column = "released"


if __name__ == "__main__":
    TodoReleases.main()
