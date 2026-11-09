"""A usage reading: what the team HUD shows of an account's rate limits, decoded wherever a frame
is decoded. The Codex bridge reads one and imports this; a client that only shows it never imports
the bridge."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Annotated

from pydantic import Field, PlainSerializer, ValidateAs

from agent_comms.cli_bridges.wire_models import DeclaredModel, WireModel, _WireFamilyMeta
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import NominalSpelling, NominalText
from agent_comms.storage.recorded_time import RecordedEventTime


class RateWindow(WireModel):
    used_percent: float = Field(alias='usedPercent')
    window_minutes: int = Field(alias='windowDurationMins')
    resets_at: Annotated[
        RecordedEventTime, ValidateAs(float, RecordedEventTime),
        PlainSerializer(lambda instant: instant.at, return_type=float),
    ] = Field(alias='resetsAt')

    def line(self) -> str:
        period = 'weekly' if self.window_minutes == 7 * 24 * 60 else f'{self.window_minutes}-minute'
        reset = time.strftime('%a %b %d %H:%M', time.localtime(self.resets_at.at))
        return f'usage {self.used_percent:g}% {period}, resets {reset}'


class RateLimitReading(DeclaredFamily, ABC, affix='RateLimitReading'):
    """A usage reading supplies its own display and need for operator attention."""

    @abstractmethod
    def line(self) -> str: ...

    def needs_hand(self) -> bool:
        return False


class CodexPlan(NominalText):
    """The subscription a reading is drawn against, as Codex spells it ("pro", "prolite"), so a
    usage line says whose limit it is."""


class CodexPlanText(NominalSpelling[CodexPlan]):
    spelling = re.compile(r"\S{1,64}")


class RateLimits(RateLimitReading, DeclaredModel, metaclass=_WireFamilyMeta):
    primary: RateWindow
    plan: CodexPlan = Field(default=CodexPlan("plan-unreported"), alias="planType")
    """Frames written before Codex reported the plan decode with it named as unreported."""

    def line(self) -> str:
        return f"({self.plan}) {self.primary.line()}"
