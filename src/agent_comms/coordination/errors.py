"""Shared durable coordination errors, independent of record store_files."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copyreg import __newobj__
from typing import ClassVar, Never, Self

from pydantic import JsonValue

from agent_comms.nominal.field_codec import DeclaredError, FieldCodec, ReportedError


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


class Refused(CommandFailure, DrainUnavailable, DeclaredError):
    """A Core refusal; JSON-RPC 2.0 section 5.1 declares its standard wire codes.

    ACP translates this value at its boundary, never inside Core's producers. Its code,
    message and data are its whole state, so a remote UI receives the refusal itself.
    """

    wire_type: ClassVar[object] = tuple[int, str, JsonValue]

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

    def to_wire(self) -> JsonValue:
        return [self.code, self.message, FieldCodec.encode(self.data)]

    @classmethod
    def from_wire(cls, data: object) -> Self:
        """Rebuilt from its declared state, never through a member's narrowed constructor:
        a member that adds state declares it by overriding this pair."""
        code, message, details = FieldCodec.decode(tuple[int, str, JsonValue], data)
        error = cls.__new__(cls)
        Refused.__init__(error, code, message, details)
        return error

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

    def retried[T](self, unchanged: T, retry: Callable[[], T]) -> T:
        """A currentness deferral waited for nothing, so its reader is not re-run on its own."""
        return unchanged


class ContendedRead(DeferredRead):  # noqa: N818 - capability, not an outcome
    """Another writer held the store through this read's whole wait budget. The same read
    can answer once that writer finishes, so retrying it is one bounded read, never a spin;
    a currentness deferral (StaleRevision) carries no such wait and is not one."""

    def retried[T](self, unchanged: T, retry: Callable[[], T]) -> T:
        return retry()


class ReportedDeferredRead(ReportedError, DeferredRead):  # noqa: N818 - reported capability
    """A deferred read reported across a boundary: still deferred, so retrying may answer it."""

    reports = DeferredRead


class CoordinationReadUnavailable(CoordinationError, DeferredRead):
    """No source snapshot was acquired; no absence is established.

    SQLite contention and an unavailable remote source retain their own error
    identities, but both leave the reader's committed value in place until the
    source announces that it can be read again.
    """

    busy_message: ClassVar[str] = "The database read is busy"
    sqlite_errorcode: int
    sqlite_errorname: str

    @classmethod
    @contextmanager
    def checking(cls) -> Iterator[None]:
        """Handle SQLite read errors; one already decoded keeps its own class."""
        try:
            yield
        except CoordinationReadUnavailable:
            raise
        except sqlite3.OperationalError as error:
            cls.raise_from(error)

    @classmethod
    def raise_from(cls, error: sqlite3.OperationalError) -> Never:
        """Decode SQLite's contention codes once, never its error wording."""
        if error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            busy = cls.contended(cls.busy_message)
            busy.sqlite_errorcode = error.sqlite_errorcode
            busy.sqlite_errorname = error.sqlite_errorname
            raise busy from error
        raise error

    @classmethod
    def contended(cls, message: str) -> CoordinationReadUnavailable:
        """SQLite's busy handler already spent this connection's busy_timeout."""
        return CoordinationReadBusy(message)


class CoordinationReadBusy(CoordinationReadUnavailable, ContendedRead):
    """A snapshot refused by SQLite BUSY or LOCKED after the busy handler's whole wait."""


class CoordinationBeginUnavailable(CoordinationReadUnavailable, sqlite3.OperationalError):
    """BEGIN acquired no write transaction; its body and commit never ran.

    It is SQLite's own refusal too, so every handler of a storage error still answers it."""

    busy_message = "The database write transaction did not start"

    @classmethod
    def contended(cls, message: str) -> CoordinationReadUnavailable:
        return cls(message)


class SchemaVersionError(CoordinationError):
    """The durable store's schema differs from its declaration."""


class IdentityConflict(CoordinationError):  # noqa: N818
    """A requested operation conflicts with frozen coordination identity."""


class UnrecordedNativeContext(IdentityConflict):  # noqa: N818 - nominal outcome name
    """A native input row recorded no context acknowledgement."""


class ReadAgain(CoordinationError):  # noqa: N818 - capability, not an outcome
    """The read's answer no longer stands, and reading again from fresh bounds can answer it.
    An observer that derives its bounds when it reads answers both members the same way:
    drop this answer and let the next observation read. Only the members differ on whether
    the same bounds may still answer (StaleRevision) or never can (ReloadRequired)."""


class StaleRevision(ReadAgain, DeferredRead, DeclaredError):  # noqa: N818 - nominal outcome name
    """The supplied CAS revision is not the authoritative revision.

    Its message is its whole state. Preserve the original retry/deferral roles
    when an observation carries it to a remote UI.
    """

    wire_type: ClassVar[object] = tuple[str]


class ReloadRequired(ReadAgain, DeclaredError):  # noqa: N818 - nominal outcome name
    """The read's own bounds name a source that is gone; retrying them never succeeds,
    so it is no DeferredRead: the caller is answered and re-reads from fresh bounds. Its message is its whole state,
    so a remote UI receives the same reload signal."""

    wire_type: ClassVar[object] = tuple[str]


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


class PublicationActivationBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """No bus-owned idempotent append acknowledgement is available in this slice."""
