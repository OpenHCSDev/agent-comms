"""Shared durable coordination errors, independent of record store_files."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from copyreg import __newobj__
from typing import Never


class CoordinationError(RuntimeError):
    """Base class for fail-loud coordinator errors."""

    def __reduce__(self):
        """Keep the original cause when an observation sends this error over its pipe."""
        return __newobj__, (type(self), *self.args), self.__dict__ | {"__cause__": self.__cause__}


class DeferredRead(Exception):  # noqa: N818 - capability, not an outcome
    """A read refused only for now, for currentness or contention; retrying may answer it."""


class CoordinationReadUnavailable(CoordinationError, DeferredRead):
    """No committed SQLite snapshot was acquired; no absence is established."""

    @classmethod
    @contextmanager
    def checking(cls) -> Iterator[None]:
        """Handle SQLite read errors."""
        try:
            yield
        except sqlite3.OperationalError as error:
            cls.raise_from(error)

    @classmethod
    def raise_from(cls, error: sqlite3.OperationalError) -> Never:
        """Decode SQLite's contention codes once, never its error wording."""
        if error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise cls("The database read is busy") from error
        raise error


class SchemaVersionError(CoordinationError):
    """The durable store's schema differs from its declaration."""


class IntegrityViolationError(CoordinationError):
    """A frozen record or declared relation is inconsistent."""


class IdentityConflict(CoordinationError):  # noqa: N818
    """A requested operation conflicts with frozen coordination identity."""


class UnrecordedNativeContext(IdentityConflict):  # noqa: N818 - nominal outcome name
    """A native input row recorded no context acknowledgement."""


class ParticipantNotRegistered(IdentityConflict):  # noqa: N818 - nominal outcome name
    """No participant aggregate is recorded for the lookup."""


class StaleRevision(CoordinationError, DeferredRead):  # noqa: N818 - nominal outcome name
    """The supplied CAS revision is not the authoritative revision."""


class ReloadRequired(StaleRevision):  # noqa: N818 - nominal outcome name
    """The read's own bounds name a source that is gone; retrying them never succeeds,
    so the caller is answered and re-reads from fresh bounds."""


class TranscriptChanged(ReloadRequired):  # noqa: N818 - nominal outcome name
    """A transcript page cursor no longer matches its thread's session file."""

    def __init__(self) -> None:
        super().__init__("Transcript changed; reload the latest page.")


class DMPaintedBoundChanged(TranscriptChanged):
    """The painted bound advanced beyond its fetched DM references."""


class DMScopeChanged(ReloadRequired):
    """A DM read belongs to another root, peer or incarnation."""


class HistoricalSourceChanged(ReloadRequired):
    """The attached archive no longer contains the displayed historical source."""


class StaleFence(CoordinationError):  # noqa: N818 - nominal outcome name
    """The owner, generation, pointer, token or attempt is no longer current."""


class RecoveryBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """A dead attempt lacks authoritative backend-final evidence."""


class RetryScheduled(CoordinationError):  # noqa: N818 - nominal outcome name
    """The settled execution is durably eligible for its next selected attempt."""


class ResponseClaimBlocked(RecoveryBlocked):
    """Response publication lacks final evidence or its exact retained route."""

    def __init__(self):
        super().__init__("response requires final model/death evidence and exact wire route")


class PublicationUncertain(CoordinationError):  # noqa: N818 - nominal outcome name
    """A frozen publishing intent has no authoritative bus-keyed acknowledgement."""


class PublicationActivationBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """No bus-owned idempotent append acknowledgement is available in this slice."""
