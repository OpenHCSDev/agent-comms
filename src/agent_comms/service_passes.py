"""Each Core service watch whose newest pass failed, readable from every process on the bus."""

from __future__ import annotations

from .root_runtime import RootRuntimeStore
import time
from dataclasses import dataclass, field, replace
from typing import ClassVar

from .locked_store import DocumentChange, LockedStore, RuntimeFile


@dataclass(frozen=True)
class FailingPass:
    """A watch failing since its first failed pass, with its newest error."""

    since: float
    error: str


@dataclass(frozen=True)
class ServicePasses:
    failing: dict[str, FailingPass] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ServicePassStore(RuntimeFile, LockedStore[ServicePasses]):
    """The connector's log reaches nobody; the commander's standing rules read this file."""
    locks_after = RootRuntimeStore

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
                    failing={
                        name: failing for name, failing in passes.failing.items() if name != watch
                    },
                ))
            )
