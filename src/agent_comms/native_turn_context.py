"""Native SDK input observations, decoded once by the existing Pi boundary."""
from dataclasses import dataclass

from .pi_payloads import PiResponseData
from .native_session_reopen import NativeSessionIdentity
from .turn_context import ContextManifest, MeasuredNativeSegment, SegmentManifest, TurnContext


@dataclass(frozen=True)
class NativeContextManifestData(PiResponseData):
    strict_fields = True
    counter: str
    segments: tuple[SegmentManifest, ...]

    def for_turn(self, thread, turn):
        return ContextManifest(thread, turn, self.segments, self.counter)


@dataclass(frozen=True)
class NativeContextData(PiResponseData):
    strict_fields = True
    counter: str
    identity: NativeSessionIdentity
    segments: tuple[MeasuredNativeSegment, ...]

    def for_turn(self, owner, turn):
        return TurnContext(owner.incarnation, turn, self.segments)

    def require_session_file(self, session_file):
        if self.identity.session_file != session_file:
            raise ValueError("Context inspection belongs to a different selected native session")
        return self
