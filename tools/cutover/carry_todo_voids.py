"""Todo rows gain the strikes voided on them (P47).

No saved strike, order or assignment is changed. Production readers accept
only the new declared schema.
"""

from __future__ import annotations

from cutover import carry


class TodoVoids(carry.TodoColumnCarry):
    """Todo rows gain the strikes voided on them; every stored strike stays as it was."""

    after = 'carry_display_counts'
    column = 'voided'


if __name__ == '__main__':
    TodoVoids.main()
