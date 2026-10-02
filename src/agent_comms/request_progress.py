"""Original native request measurements; no admission, completion or retry authority."""
from dataclasses import dataclass, field

from .pi_payloads import PiPayload


@dataclass(frozen=True)
class RequestProgress(PiPayload):
    request_id: str = field(metadata={"wire_name": "requestId"})
    session_id: str = field(metadata={"wire_name": "sessionId"})
    input_id: str = field(metadata={"wire_name": "inputId"})
    started_at_ms: int = field(metadata={"wire_name": "startedAtMs"})
    observed_at_ms: int = field(metadata={"wire_name": "observedAtMs"})
    monotonic_ns: str = field(metadata={"wire_name": "monotonicNs"})
    elapsed_ms: float = field(metadata={"wire_name": "elapsedMs"})
    callback_ms: float = field(metadata={"wire_name": "callbackMs"})
    callback_count: int = field(metadata={"wire_name": "callbackCount"})
    callback_max_ms: float = field(metadata={"wire_name": "callbackMaxMs"})
    stage: str
    detail: str = ""
    transport: str = ""
    attempt: int = 0
    status: int | None = None
    response_id: str | None = field(default=None, metadata={"wire_name": "responseId"})
    callback: str = ""

    def __post_init__(self):
        if min(self.started_at_ms, self.observed_at_ms, self.elapsed_ms,
               self.callback_ms, self.callback_count, self.callback_max_ms) < 0:
            raise ValueError("Invalid native request measurement")
        if not self.monotonic_ns.isdecimal():
            raise ValueError("Invalid native monotonic observation")

    @property
    def label(self):
        return self.detail[:160] or "Waiting for model"
