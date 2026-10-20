"""Add prior todo declarations before any step reads current todo rows."""

from .carry import TodoColumnCarry


class TodoOrderHistory(TodoColumnCarry):
    column = "prior_orders"


if __name__ == "__main__":
    TodoOrderHistory.main()
