"""Add prior todo declarations before any step reads current todo rows."""

from agent_comms.installer.carry import TodoColumnCarry


class TodoOrderHistory(TodoColumnCarry):
    column = "prior_orders"
