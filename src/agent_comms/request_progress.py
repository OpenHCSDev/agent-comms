"""Native request measurements do not permit input, confirm completion or allow retry."""

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from agent_comms import field_codec

from .child_process import ProcessIdentity
from .pi_counts import PiCount, UnreportedCount
from .pi_payloads import (
    PiModel,
    PiPayload,
    UnreportedModel,
)
from .turn_item import NativeTurnIdText


@dataclass(frozen=True, slots=True)
class UnreportedText(field_codec.NullMember):
    """Pi did not report this text observation for the request."""


@dataclass(frozen=True, slots=True)
class UnreportedIteratorTiming(field_codec.NullMember):
    """Original record has no iterator timing, not a measured zero."""


class IteratorStreamId(field_codec.NominalText):
    """One original provider iterator, distinct from its request and session."""


class IteratorStreamIdText(field_codec.NominalSpelling[IteratorStreamId]):
    spelling = NativeTurnIdText.spelling


@dataclass(frozen=True)
class IteratorTiming(PiPayload):
    """Iterator waiting/yield wall times, not server/network/CPU attribution."""

    stream_id: Annotated[IteratorStreamId, field_codec.WireName("streamId")]
    transport: str
    phase: Literal["iteratorAwait", "consumerYield", "closed"]
    started_monotonic_ns: Annotated[str, field_codec.WireName("startedMonotonicNs")]
    observed_monotonic_ns: Annotated[str, field_codec.WireName("observedMonotonicNs")]
    iterator_await_ms: Annotated[float, field_codec.WireName("iteratorAwaitMs")]
    consumer_yield_ms: Annotated[float, field_codec.WireName("consumerYieldMs")]
    event_count: Annotated[int, field_codec.WireName("eventCount")]

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) or value < 0
               for value in (self.iterator_await_ms, self.consumer_yield_ms)):
            raise ValueError("Invalid iterator duration")
        if self.event_count < 0:
            raise ValueError("Invalid iterator event count")
        if not (self.started_monotonic_ns.isdecimal() and self.observed_monotonic_ns.isdecimal()):
            raise ValueError("Invalid iterator monotonic observation")
        if int(self.observed_monotonic_ns) < int(self.started_monotonic_ns):
            raise ValueError("Iterator observation precedes its source")


@dataclass(frozen=True)
class RequestProgress(PiPayload):
    request_id: Annotated[str, field_codec.WireName("requestId")]
    session_id: Annotated[str, field_codec.WireName("sessionId")]
    started_at_ms: Annotated[int, field_codec.WireName("startedAtMs")]
    observed_at_ms: Annotated[int, field_codec.WireName("observedAtMs")]
    monotonic_ns: Annotated[str, field_codec.WireName("monotonicNs")]
    elapsed_ms: Annotated[float, field_codec.WireName("elapsedMs")]
    callback_ms: Annotated[float, field_codec.WireName("callbackMs")]
    callback_count: Annotated[int, field_codec.WireName("callbackCount")]
    callback_max_ms: Annotated[float, field_codec.WireName("callbackMaxMs")]
    stage: str
    iterator_timing: Annotated[
        IteratorTiming | UnreportedIteratorTiming,
        field_codec.WireName("iteratorTiming"), field_codec.WireOmitDefault(),
    ] = UnreportedIteratorTiming()
    detail: str = ""
    transport: str = ""
    attempt: int = 0
    status: int | None = None
    response_id: Annotated[str | UnreportedText, field_codec.WireName("responseId")] = (
        UnreportedText()
    )
    callback: str = ""
    # External request observations are optional at other stages and in original
    # records. Absence is unavailable evidence, never a registry/catalog lookup.
    model: Annotated[PiModel, field_codec.WireOmitDefault()] = UnreportedModel()
    estimated_input_tokens: Annotated[
        PiCount, field_codec.WireName("estimatedInputTokens"), field_codec.WireOmitDefault()
    ] = UnreportedCount()
    available_tokens: Annotated[
        PiCount, field_codec.WireName("availableTokens"), field_codec.WireOmitDefault()
    ] = UnreportedCount()
    output_token_field: Annotated[
        str | UnreportedText, field_codec.WireName("outputTokenField"),
        field_codec.WireOmitDefault(),
    ] = UnreportedText()
    requested_output_tokens: Annotated[
        PiCount, field_codec.WireName("requestedOutputTokens"), field_codec.WireOmitDefault()
    ] = UnreportedCount()
    allowed_output_tokens: Annotated[
        PiCount, field_codec.WireName("admittedOutputTokens"), field_codec.WireOmitDefault()
    ] = UnreportedCount()
    minimum_output_tokens: Annotated[
        PiCount, field_codec.WireName("minimumOutputTokens"), field_codec.WireOmitDefault()
    ] = UnreportedCount()

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
