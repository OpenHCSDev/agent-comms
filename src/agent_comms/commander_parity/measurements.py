"""Observe measurement owners through the standing-rule delivery they supply."""

from __future__ import annotations

from dataclasses import dataclass
from sys import monitoring
from types import CodeType, MethodType

from ..commander.config import CommanderConfig
from ..comms import Comms
from ..standing_rules import DiskLow, StandingRule
from ..storage_rules import UnitStorage
from . import ParityComparison, UnverifiedParity
from .load import state_files


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
        return (
            f"owner={self.owner}; native_calls={self.calls}; "
            f"current_count={len(self.current)}; native_count={len(self.native)}; "
            f"equal={self.current == self.native}; "
            f"private_state_unchanged={self.private_state_unchanged}"
        )

    @classmethod
    def read(cls, owner: type[StandingRule]) -> ParityComparison:
        method = owner.violations
        if not isinstance(method, MethodType):
            return UnverifiedParity(f"{owner.__qualname__}.violations is not a bound classmethod")
        code = method.__func__.__code__
        config = CommanderConfig.read(CommanderConfig.path())
        comms = Comms(config.root)
        before = state_files(config.state)
        observed: list[tuple[StandingRule, ...]] = []

        def observe(_code: CodeType, _offset: int, value: tuple[StandingRule, ...]) -> None:
            observed.append(value)

        # Subscribe to the owner's real return event, not string-dispatched profile traffic.
        monitoring.use_tool_id(monitoring.PROFILER_ID, __name__)
        try:
            monitoring.register_callback(
                monitoring.PROFILER_ID, monitoring.events.PY_RETURN, observe
            )
            monitoring.set_local_events(monitoring.PROFILER_ID, code, monitoring.events.PY_RETURN)
            current = owner.broken(comms, config.commander)
        finally:
            monitoring.free_tool_id(monitoring.PROFILER_ID)
        if len(observed) != 1:
            return UnverifiedParity(
                f"Expected one {owner.__qualname__}.violations call: {len(observed)}"
            )
        native = tuple(rule for rule in observed[0] if rule.broke_after(0.0))
        return cls(
            f"{owner.__module__}.{owner.__qualname__}",
            tuple(rule.line() for rule in current),
            tuple(rule.line() for rule in native),
            len(observed),
            before == state_files(config.state),
        )


@dataclass(frozen=True)
class StorageComparison(ParityComparison):
    quota: ParityComparison
    disk: ParityComparison

    @property
    def passed(self) -> bool:
        return self.quota.passed and self.disk.passed

    def describe(self) -> str:
        return f"quota: {self.quota.describe()}; disk: {self.disk.describe()}"

    @classmethod
    def read(cls) -> StorageComparison:
        return cls(RuleComparison.read(UnitStorage), RuleComparison.read(DiskLow))
