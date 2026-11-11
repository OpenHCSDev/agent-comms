"""A runtime request as its owner's server receives it: encoded by the client and decoded
through the family, which yields the served class wherever the served module is imported."""

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.transport.runtime_request import RuntimeRequest


def served[Request: RuntimeRequest](request: Request) -> Request:
    return FieldCodec.decode(RuntimeRequest, FieldCodec.encode(request))
