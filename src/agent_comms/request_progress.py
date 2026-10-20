"""Native request measurements do not permit input, confirm completion or allow retry."""

from typing import Annotated
from collections.abc import Callable
from dataclasses import dataclass

from agent_comms import field_codec

from .child_process import ProcessIdentity
from .pi_payloads import PiModel, PiPayload


@dataclass(frozen=True)
class RequestProgress(PiPayload):
    request_id: Annotated[str, field_codec.WireName("requestId")]
    session_id: Annotated[str, field_codec.WireName("sessionId")]
    input_id: Annotated[str, field_codec.WireName("inputId")]
    started_at_ms: Annotated[int, field_codec.WireName("startedAtMs")]
    observed_at_ms: Annotated[int, field_codec.WireName("observedAtMs")]
    monotonic_ns: Annotated[str, field_codec.WireName("monotonicNs")]
    elapsed_ms: Annotated[float, field_codec.WireName("elapsedMs")]
    callback_ms: Annotated[float, field_codec.WireName("callbackMs")]
    callback_count: Annotated[int, field_codec.WireName("callbackCount")]
    callback_max_ms: Annotated[float, field_codec.WireName("callbackMaxMs")]
    stage: str
    detail: str = ""
    transport: str = ""
    attempt: int = 0
    status: int | None = None
    response_id: Annotated[str | None, field_codec.WireName("responseId")] = None
    callback: str = ""
    # External request observations are optional at other stages and in original
    # records. Absence is unavailable evidence, never a registry/catalog lookup.
    model: Annotated[PiModel | None, field_codec.WireOmitDefault()] = None
    estimated_input_tokens: Annotated[
        int | None, field_codec.WireName("estimatedInputTokens"), field_codec.WireOmitDefault()
    ] = None
    available_tokens: Annotated[
        int | None, field_codec.WireName("availableTokens"), field_codec.WireOmitDefault()
    ] = None
    output_token_field: Annotated[
        str | None, field_codec.WireName("outputTokenField"), field_codec.WireOmitDefault()
    ] = None
    requested_output_tokens: Annotated[
        int | None, field_codec.WireName("requestedOutputTokens"), field_codec.WireOmitDefault()
    ] = None
    allowed_output_tokens: Annotated[
        int | None, field_codec.WireName("admittedOutputTokens"), field_codec.WireOmitDefault()
    ] = None
    minimum_output_tokens: Annotated[
        int | None, field_codec.WireName("minimumOutputTokens"), field_codec.WireOmitDefault()
    ] = None

    def __post_init__(self) -> None:
        if min(self.started_at_ms, self.observed_at_ms, self.elapsed_ms,
               self.callback_ms, self.callback_count, self.callback_max_ms) < 0:
            raise ValueError("Invalid native request measurement")
        if not self.monotonic_ns.isdecimal():
            raise ValueError("Invalid native monotonic observation")

    @property
    def label(self) -> str:
        return self.detail[:160] or "Waiting for model"


RequestObserver = Callable[[RequestProgress, ProcessIdentity], None]


class NoRequestObserver:
    def __call__(self, progress: RequestProgress, process: ProcessIdentity) -> None:
        pass


NO_REQUEST_OBSERVER = NoRequestObserver()
