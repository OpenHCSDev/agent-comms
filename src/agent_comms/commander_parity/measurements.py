"""Observe measurement owners through the standing-rule delivery they supply."""

from __future__ import annotations

from dataclasses import dataclass
from sys import monitoring
from types import CodeType, MethodType

from agent_comms.commander.config import CommanderConfig
from agent_comms.commander_parity import ParityComparison, UnverifiedParity
from agent_comms.commander_parity.load import LoadComparison, state_files
from agent_comms.comms import Comms
from agent_comms.standing_rules import DiskLow, StandingRule
from agent_comms.storage_rules import StorageViolation
from agent_comms.todos import PullRequests
from agent_comms.ui_model.unit_state import UnitState


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
        method = owner.scan_observed
        if not isinstance(method, MethodType):
            return UnverifiedParity(
                f"{owner.__qualname__}.scan_observed is not a bound classmethod"
            )
        code = method.__func__.__code__
        config = CommanderConfig.read(CommanderConfig.path())
        comms = Comms(config.root)
        states = UnitState.current(comms, config.commander, PullRequests())
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
            current = owner.broken(comms, config.commander, observed=states)
        finally:
            monitoring.free_tool_id(monitoring.PROFILER_ID)
        if len(observed) != 1:
            return UnverifiedParity(
                f"Expected one {owner.__qualname__}.scan_observed call: {len(observed)}"
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
    units: ParityComparison
    disk: ParityComparison

    @property
    def passed(self) -> bool:
        return self.units.passed and self.disk.passed

    def describe(self) -> str:
        return f"units: {self.units.describe()}; disk: {self.disk.describe()}"

    @classmethod
    def read(cls) -> StorageComparison:
        return cls(RuleComparison.read(StorageViolation.scan_owner()), RuleComparison.read(DiskLow))


def measured_load() -> ParityComparison:
    observed = LoadComparison.read()
    return UnverifiedParity(
        f"Live Load comparison passed={observed.passed}: {observed.describe()}; "
        "missing native self-report comparison owner"
    )
