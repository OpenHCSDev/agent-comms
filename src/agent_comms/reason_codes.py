"""Why a coordination row last changed, when the change names a reason."""
from __future__ import annotations

from dataclasses import dataclass

from agent_comms.coordination_contracts import MAX_REASON_CODE_CHARS, MAX_SANITIZED_DETAIL_CHARS
from agent_comms.field_codec import SubclassUnion
from agent_comms.thread_text import AbsentText, GivenText, TextChoice


class ReasonCode(SubclassUnion, TextChoice, affix="ReasonCode"):
    """A bounded reason, stored as its text, or SQL NULL when the change named none."""


@dataclass(frozen=True, slots=True)
class GivenReasonCode(GivenText, ReasonCode):
    text: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.text) <= MAX_REASON_CODE_CHARS:
            raise ValueError("reason code is invalid")


@dataclass(frozen=True, slots=True)
class NoReasonCode(AbsentText, ReasonCode):
    pass


class RecoveryDetail(SubclassUnion, TextChoice, affix="Detail"):
    """A recovery audit's sanitized detail, stored as its text, or SQL NULL when none was kept."""


@dataclass(frozen=True, slots=True)
class GivenDetail(GivenText, RecoveryDetail):
    text: str

    def __post_init__(self) -> None:
        if len(self.text) > MAX_SANITIZED_DETAIL_CHARS:
            raise ValueError("sanitized_detail exceeds its bound")


@dataclass(frozen=True, slots=True)
class NoDetail(AbsentText, RecoveryDetail):
    pass
