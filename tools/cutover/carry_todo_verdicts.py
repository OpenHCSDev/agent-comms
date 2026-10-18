"""Todo rows gain the commander's verdicts on the holder's tool calls (P81).

No saved strike, order or assignment is changed. Production readers accept
only the new declared schema.
"""

from __future__ import annotations

from cutover import carry


class TodoVerdicts(carry.TodoColumnCarry):
    """Todo rows gain the empty verdicts record; every stored strike stays as it was."""

    after = "carry_todo_releases"
    column = "verdicts"


if __name__ == "__main__":
    TodoVerdicts.main()
