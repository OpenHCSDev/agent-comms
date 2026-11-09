"""Decode stored order phases without pre-importing their concrete declarations."""

import sys

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.planning.order_phase import OrderPhase
from agent_comms.planning.orders import PendingProblemOrder


def main() -> None:
    print("Plan definitions loaded before schema:", 'agent_comms.planning.plan_phases' in sys.modules, flush=True)
    # The real stored order schema must acquire its phase declarations too.
    FieldCodec.record_schema(PendingProblemOrder)
    schema = FieldCodec.value_schema(type[OrderPhase])
    for name in sys.argv[1:]:
        assert name in schema["enum"], schema
        decoded = FieldCodec.decode(type[OrderPhase], name)
        assert decoded.declared_name == name
        assert FieldCodec.encode(decoded) == name
    print("Cold order schema and stored phase names resolve", flush=True)


if __name__ == "__main__":
    main()
