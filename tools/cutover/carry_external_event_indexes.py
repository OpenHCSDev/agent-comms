"""P717: allow the shared index step to reconcile the event declaration."""

from agent_comms.external_transcript import ExternalEventRow

from .carry import StoreCarry
from .carry_tool_call_index import ToolCallIndex


class ExternalEventIndexes(ToolCallIndex, StoreCarry):
    allowed_indexes = frozenset(ExternalEventRow.schema_objects()) - {
        ExternalEventRow.declared_name
    }


if __name__ == "__main__":
    ExternalEventIndexes.main()
