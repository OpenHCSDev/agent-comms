"""Observe measurement owners through the standing-rule delivery they supply."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import FrameType

from ..commander.config import CommanderConfig
from ..comms import Comms
from ..standing_rules import DiskLow, StandingRule
from ..storage_rules import UnitStorage
from . import ParityComparison, UnverifiedParity
from .load import LoadComparison, state_files


@dataclass(frozen=True)
class RuleComparison(ParityComparison):
    owner: str
    current: tuple[str, ...]
    native: tuple[str, ...]
    calls: int
    private_state_unchanged: bool

    @property
    def passed(self) -> bool:
        return self.calls == 1 and self.current == self.native and self.private_state_unchanged

    def describe(self) -> str:
        return (f"owner={self.owner}; native_calls={self.calls}; "
                f"current_count={len(self.current)}; native_count={len(self.native)}; "
                f"equal={self.current == self.native}; "
                f"private_state_unchanged={self.private_state_unchanged}")

    @classmethod
    def read(cls, owner: type[StandingRule]) -> RuleComparison:
        config = CommanderConfig.read(CommanderConfig.path())
        comms = Comms(config.root)
        before = state_files(config.state)
        observed: list[tuple[StandingRule, ...]] = []
        method = owner.violations.__func__.__code__

        def observe(frame: FrameType, event: str, value: tuple[StandingRule, ...]) -> None:
            if event == "return" and frame.f_code is method:
                observed.append(value)

        previous = sys.getprofile()
        sys.setprofile(observe)
        try:
            current = owner.broken(comms, config.commander)
        finally:
            sys.setprofile(previous)
        native = tuple(rule for returned in observed for rule in returned if rule.broke_after(0.0))
        return cls(f"{owner.__module__}.{owner.__qualname__}",
                   tuple(rule.line() for rule in current), tuple(rule.line() for rule in native),
                   len(observed), before == state_files(config.state))


@dataclass(frozen=True)
class StorageComparison(ParityComparison):
    quota: RuleComparison
    disk: RuleComparison

    @property
    def passed(self) -> bool:
        return self.quota.passed and self.disk.passed

    def describe(self) -> str:
        return f"quota: {self.quota.describe()}; disk: {self.disk.describe()}"

    @classmethod
    def read(cls) -> StorageComparison:
        return cls(RuleComparison.read(UnitStorage), RuleComparison.read(DiskLow))


def measured_load() -> ParityComparison:
    observed = LoadComparison.read()
    return UnverifiedParity(
        f"Live Load comparison passed={observed.passed}: {observed.describe()}; "
        "missing native self-report comparison owner")
