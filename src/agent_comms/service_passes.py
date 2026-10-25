"""Original completion times and current failures of Core's service watches."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import ClassVar

from .locked_store import DocumentChange, DurableFile, LockedStore
from .store_files import DependentStateTier, SingleFileStore
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation


@dataclass(frozen=True)
class FailingPass:
    """A watch failing since its first failed pass, with its newest error."""

    since: float
    error: str


@dataclass(frozen=True)
class ServicePasses:
    failing: dict[str, FailingPass] = field(default_factory=dict)
    commander_completed: dict[RecordedThreadIncarnation, float] = field(default_factory=dict)
    """Last observation completed for each commander, including quiet observations."""


@dataclass(frozen=True, slots=True)
class ServicePassStore(SingleFileStore, DurableFile, LockedStore[ServicePasses], DependentStateTier):
    """The connector's log reaches nobody; the commander's standing rules read this file."""

    filename: ClassVar[str] = "service_passes.json"
    record_type = ServicePasses

    def empty(self) -> ServicePasses:
        return ServicePasses()

    def failed(self, watch: str, error: BaseException) -> None:
        def change(passes: ServicePasses) -> DocumentChange[ServicePasses, ServicePasses]:
            since = passes.failing.get(watch, FailingPass(time.time(), "")).since
            failing = FailingPass(since, f"{type(error).__name__}: {error}")
            return DocumentChange.to(replace(passes, failing=passes.failing | {watch: failing}))

        self.update(change)

    def passed(self, watch: str) -> None:
        """A pass that succeeds ends its failure; an unfailed watch writes nothing."""
        if watch in self.read().failing:
            self.update(
                lambda passes: DocumentChange.to(replace(
                    passes,
                    failing={name: failure for name, failure in passes.failing.items() if name != watch},
                ))
            )

    def heartbeat_passed(
        self, commanders: Iterable[ThreadIncarnation], registered: Iterable[ThreadIncarnation]
    ) -> None:
        """Only a retired incarnation loses its completion; a missed live one keeps it."""
        current = frozenset(registered)
        completed = {owner.require_recorded(): time.time() for owner in commanders}

        def change(passes: ServicePasses) -> DocumentChange[ServicePasses, ServicePasses]:
            retained = {owner: at for owner, at in passes.commander_completed.items()
                        if owner in current}
            updated = retained | completed
            return DocumentChange.to(
                passes if updated == passes.commander_completed
                else replace(passes, commander_completed=updated)
            )

        self.update(change)
