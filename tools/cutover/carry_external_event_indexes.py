"""P717: install newly declared external-event indexes without rewriting events."""

from .carry import StoreCarry
from .carry_tool_call_index import ToolCallIndex


class ExternalEventIndexes(ToolCallIndex, StoreCarry):
    """A new ledger step reuses the strict declared-index installer after P286."""


if __name__ == "__main__":
    ExternalEventIndexes.main()
