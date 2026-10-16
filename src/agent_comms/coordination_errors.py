"""Shared durable coordination errors, independent of record store_files."""

import sqlite3
from abc import ABC, abstractmethod
from typing import Never


class CoordinationError(RuntimeError):
    """Base class for fail-loud coordinator errors."""


class ReadErrorBehavior(ABC):
    @abstractmethod
    def watch_delay(self) -> float: ...

    @abstractmethod
    def raise_failure(self, operation: str) -> Never: ...


class WaitRead(ReadErrorBehavior):
    """The same busy-read behavior in Python and on the CLI connection."""

    def watch_delay(self) -> float:
        return 0.25


class FailRead(ReadErrorBehavior):
    """A failed read must stop; its exception or wire message supplies the error."""

    def watch_delay(self) -> float:
        self.raise_failure("watch")


class CoordinationReadUnavailable(CoordinationError, ReadErrorBehavior):
    """A read did not finish; the declared member decides whether to wait or fail."""

    @classmethod
    def raise_from(cls, error: sqlite3.DatabaseError) -> Never:
        """Decode SQLite's result code once, never its error wording."""
        member = (CoordinationReadBusy
                  if error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                  else CoordinationReadFailed)
        raise member(str(error)) from error

    def raise_failure(self, operation: str) -> Never:
        raise self


class CoordinationReadBusy(WaitRead, CoordinationReadUnavailable):
    """Another reader or writer is working; a later read may finish."""


class CoordinationReadFailed(FailRead, CoordinationReadUnavailable):
    """The store could not be read; retrying must not hide that failure."""


class SchemaVersionError(CoordinationError):
    """The durable store has an unsupported schema version."""


class IntegrityViolationError(CoordinationError):
    """A frozen record or declared relation is inconsistent."""


class IdentityConflict(CoordinationError):  # noqa: N818
    """A requested operation conflicts with frozen coordination identity."""


class StaleRevision(CoordinationError):  # noqa: N818 - nominal outcome name
    """The supplied CAS revision is not the authoritative revision."""


class TranscriptChanged(StaleRevision):  # noqa: N818 - nominal outcome name
    """A transcript page cursor no longer matches its thread's session file."""

    def __init__(self) -> None:
        super().__init__("Transcript changed; reload the latest page.")


class StaleFence(CoordinationError):  # noqa: N818 - nominal outcome name
    """The owner, generation, pointer, token or attempt is no longer current."""


class RecoveryBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """A dead attempt lacks authoritative backend-final evidence."""


class RetryScheduled(CoordinationError):  # noqa: N818 - nominal outcome name
    """The settled execution is durably eligible for its next selected attempt."""


class ResponseAdmissionBlocked(RecoveryBlocked):
    """Response publication lacks final evidence or its exact retained route."""

    def __init__(self):
        super().__init__("response requires final model/death evidence and exact wire route")


class PublicationUncertain(CoordinationError):  # noqa: N818 - nominal outcome name
    """A frozen publishing intent has no authoritative bus-keyed receipt."""


class PublicationActivationBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """No bus-owned idempotent append receipt is available in this slice."""
