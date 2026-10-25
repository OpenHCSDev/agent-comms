"""Shared durable coordination errors, independent of record store_files."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from copyreg import __newobj__
from typing import ClassVar, Never, Self


class DrainDeferred(Exception):  # noqa: N818 - capability, not an outcome
    """A failure an inbox drain records and retries later instead of stopping on."""


class CommandFailure(Exception):  # noqa: N818 - capability, not an outcome
    """A failure a command reports to its caller as a failed result instead of crashing.

    It crosses a worker's pipe as its class, args and state, never through a member's own
    ``__init__``, whose parameters are not its args, and keeps its original cause."""

    def __reduce__(self):
        return __newobj__, (type(self), *self.args), self.__dict__ | {"__cause__": self.__cause__}


class DrainUnavailable(DrainDeferred):
    """A failure meaning the drain's store or coordinator is unavailable right now."""


class Refused(CommandFailure, DrainUnavailable):
    """A Core refusal; JSON-RPC 2.0 section 5.1 declares its standard wire codes.

    ACP translates this value at its boundary, never inside Core's producers.
    """

    INVALID_PARAMS: ClassVar[int] = -32602
    METHOD_NOT_FOUND: ClassVar[int] = -32601
    INTERNAL_ERROR: ClassVar[int] = -32603

    code: int
    data: object

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code, self.data = code, data

    @property
    def message(self) -> str:
        return str(self)

    @classmethod
    def invalid_params(cls, data: object = None) -> Self:
        return cls(cls.INVALID_PARAMS, "Invalid params", data)

    @classmethod
    def method_not_found(cls, method: str) -> Self:
        return cls(cls.METHOD_NOT_FOUND, "Method not found", {"method": method})


class CoordinationError(RuntimeError, DrainUnavailable, CommandFailure):
    """Base class for fail-loud coordinator errors."""


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
