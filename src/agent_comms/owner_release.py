"""Completed owner releases, retirement checks and their durable store."""

from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import (
    Callable,
    Iterator,
    Sequence,
)
from contextlib import (
    closing,
    contextmanager,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from .child_process import ProcessState
from .nominal.declared_family import DeclaredFamily
from .errors import RelationViolationError
from .registry_document import RegistrySnapshot
from .thread_identity import (
    ClaimedIdentity,
    OwnerIdentity,
    ThreadIncarnationRecord,
)
from .thread_owned_state import ThreadOwnedState
from .threads import Thread
from .typed_table import (
    Column,
    DurableStore,
    JsonStorage,
    StoreRows,
    TypedTable,
)


class OwnerRelease(ABC):
    """An exact release or its absence; neither grants permission to replay."""
    @abstractmethod
    def current(self, snapshot, expected, claim_scope) -> bool: ...
    @abstractmethod
    def matches(self, expected) -> bool: ...
    @abstractmethod
    def require_owner_loss(self, snapshot, owner_identity): ...

    def retirement(self, snapshot: RegistrySnapshot, original: Thread, before: int) -> OwnerCompletion:
        raise RelationViolationError("Completed owner retirement is missing")

    def require_native_loss(self, snapshot, source):
        release = self.require_owner_loss(snapshot, source.owner_identity)
        current = snapshot.require(release.name)
        source.sent_owner_claimed_generation.require_release(release, snapshot, current)
        return release

    def require_stopping_owner(self, snapshot: RegistrySnapshot, thread: Thread,
                               claimed_generation: int) -> None:
        if self.current(snapshot, thread, claimed_generation):
            return
        try:
            snapshot.require_stopping_owner(thread, claimed_generation)
        except RelationViolationError as error:
            raise RelationViolationError(
                f"Owner changed while stopping {thread.name!r}; refusing a stale signal."
            ) from error


class NoRelease(OwnerRelease):
    def current(self, snapshot, expected, claim_scope) -> bool:
        return False

    def matches(self, expected) -> bool:
        return False

    def require_owner_loss(self, snapshot, owner_identity):
        raise RelationViolationError("native owner release acknowledgement is missing")


@dataclass(frozen=True)
class ReleasedOwner(ThreadIncarnationRecord):
    process_identity: Annotated[ProcessState, Column(storage=JsonStorage, nullable=True)]

    def matches(self, expected: Thread) -> bool:
        return self.incarnation() == expected.incarnation and self.process_identity == expected.process_identity


class OwnerReleaseStoreRows(StoreRows):
    """The tables of OwnerReleaseStore; each row declares itself one."""


class OwnerCompletion(DeclaredFamily, affix="OwnerCompletion"):
    @abstractmethod
    def require_loss(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> None: ...

    @abstractmethod
    def require_retired(self, snapshot: RegistrySnapshot, thread: Thread,
                        process: ProcessState) -> None:
        """``thread`` is the registry's current declaration; ``process`` is the released one."""

    def matches_current(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> bool:
        """The row still binds the released process or, once a dead owner's stop
        unregistered it, none; a different process is a successor."""
        current = snapshot.require(release.name)
        return (release.incarnation() == current.incarnation
                and not current.process_identity.conflicts_with(release.process_identity))


@dataclass(frozen=True)
class ProcessExitedOwnerCompletion(OwnerCompletion):
    def require_retired(self, snapshot: RegistrySnapshot, thread: Thread,
                        process: ProcessState) -> None:
        # The registry still binds the released process (a fenced self-release) or none
        # (a stopped owner was unregistered). A release that names no process was
        # recorded only after its stop saw the owner dead: nothing of it is alive.
        if thread.process_identity.conflicts_with(process):
            raise RelationViolationError("live owner is not the current process incarnation")
        if process.alive():
            raise RelationViolationError("Original owner survived retirement")

    def require_loss(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> None:
        if release.process_identity.alive():
            raise RelationViolationError("Released native owner process is still alive")
        current = snapshot.require(release.name)
        if release.matches(current):
            snapshot.status(current.name).require_inactive("Released native process is still the registry owner")
        if (snapshot.claim_generation(current.name) == release.after
                and current.process_identity.conflicts_with(release.process_identity)):
            raise RelationViolationError("Native release process differs from its original declaration")


@dataclass(frozen=True)
class SessionRetiredOwnerCompletion(OwnerCompletion):
    owner: OwnerIdentity

    def require_retired(self, snapshot: RegistrySnapshot, thread: Thread,
                        process: ProcessState) -> None:
        current = snapshot.owner_identity(thread.name)
        if current.incarnation != self.owner.incarnation or current.generation <= self.owner.generation:
            raise RelationViolationError("Retired session still holds its original owner lease")
        if not thread.process_identity.allows_owner_start():
            raise RelationViolationError("Retired session acquired another process")

    def require_loss(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> None:
        if self.owner.incarnation != release.incarnation():
            raise RelationViolationError("Retired session belongs to another incarnation")
        if snapshot.owner_identity(release.name).generation <= self.owner.generation:
            raise RelationViolationError("Retired session still holds its original owner lease")

    def matches_current(self, snapshot: RegistrySnapshot, release: OwnerReleaseEvidence) -> bool:
        self.require_loss(snapshot, release)
        return release.incarnation().current(snapshot)


@dataclass(frozen=True)
class OwnerReleaseEvidence(OwnerReleaseStoreRows, ReleasedOwner, OwnerRelease, TypedTable):
    name: Annotated[str, Column(primary_key=True)]
    before: int
    after: int
    completion: Annotated[OwnerCompletion, Column()] = ProcessExitedOwnerCompletion()

    def __post_init__(self) -> None:
        if self.before <= 0 or self.after <= self.before:
            raise ValueError("Owner release requires an advanced claim_scope and a closed turn")

    def current(self, snapshot: RegistrySnapshot, expected: Thread, claim_scope: int) -> bool:
        if self.before != claim_scope or not self.matches(expected):
            return False
        if not self.incarnation().current(snapshot) or not self.completion.matches_current(snapshot, self):
            return False
        return self.after == snapshot.claim_generation(self.name) and snapshot.status(self.name).stopped

    def retirement(self, snapshot: RegistrySnapshot, original: Thread, before: int) -> OwnerCompletion:
        if not self.current(snapshot, original, before):
            raise RelationViolationError("Completed retirement differs from the selected original owner")
        self.completion.require_loss(snapshot, self)
        return self.completion

    def require_retired_for(self, snapshot: RegistrySnapshot) -> None:
        """Judge the registry's current declaration, never a caller's pre-stop copy: a
        retired session unregistered its process, so the released process is what retired."""
        self.completion.require_retired(snapshot, snapshot.require(self.name), self.process_identity)

    def require_owner_loss(self, snapshot, owner_identity):
        """Attest only this recorded owner's release and dead process.

        Input generation belongs to the recorded input, not to owner-loss proof.
        Neither this release nor missing input proves bytes were unwritten.
        """
        if not self.incarnation().current(snapshot):
            raise RelationViolationError("Released native owner incarnation changed")
        current = snapshot.require(self.name)
        owner_identity.require_recorded_owner(current)
        self.completion.require_loss(snapshot, self)
        actual = snapshot.claimed_identity(current.name)
        released = ClaimedIdentity(self.incarnation(), self.after)
        if not actual.includes(released):
            raise RelationViolationError("Native release claim_scope regressed")
        return self


@dataclass(frozen=True)
class OwnerReleaseStore(ThreadOwnedState, DurableStore):
    root: Path
    filename = "owner_releases.sqlite3"
    tables = OwnerReleaseStoreRows

    @contextmanager
    def reading(self) -> Iterator[Callable[[str], OwnerRelease]]:
        with self.snapshot() as db:
            def read(name: str) -> OwnerRelease:
                with closing(OwnerReleaseEvidence.stream(db, name=name)) as records:
                    return next(records, NoRelease())
            yield read

    def read_many(self, names: Sequence[str]) -> dict[str, OwnerRelease]:
        with self.reading() as read:
            return {name: read(name) for name in names}

    def record(self, snapshot: RegistrySnapshot, owners: Sequence[tuple[Thread, int]]) -> None:
        if not owners:
            return
        with self.writing() as db:
            for original, before in owners:
                # A stopped dead owner is already unregistered in `snapshot`: the release
                # names the process the original held, as record_session does.
                original.turn_record.read().require_idle()
                OwnerReleaseEvidence(original.name, original.incarnation.wire_birth,
                                     original.process_identity, before,
                                     snapshot.claim_generation(original.name)).upsert(db)

    def record_session(
        self, snapshot: RegistrySnapshot, original: Thread, before: int, owner: OwnerIdentity,
    ) -> None:
        original.turn_record.read().require_idle()
        with self.writing() as db:
            OwnerReleaseEvidence(
                original.name, original.incarnation.wire_birth, original.process_identity,
                before, snapshot.claim_generation(original.name), SessionRetiredOwnerCompletion(owner),
            ).upsert(db)

    def remove_threads(self, threads: Sequence[Thread]) -> None:
        with self.writing() as db:
            for thread in threads:
                OwnerReleaseEvidence.delete(db, name=thread.name)
