"""Shared durable coordination errors, independent of record store_files."""


class CoordinationError(RuntimeError):
    """Base class for fail-loud coordinator errors."""


class SchemaVersionError(CoordinationError):
    """The durable store has an unsupported schema version."""


class IntegrityViolationError(CoordinationError):
    """A frozen record or declared relation is inconsistent."""


class IdentityConflict(CoordinationError):  # noqa: N818
    """A requested operation conflicts with frozen coordination identity."""


class StaleRevision(CoordinationError):  # noqa: N818 - nominal outcome name
    """The supplied CAS revision is not the authoritative revision."""


class StaleFence(CoordinationError):  # noqa: N818 - nominal outcome name
    """The owner, generation, pointer, token or attempt is no longer current."""


class RecoveryBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """A dead attempt lacks authoritative backend-final evidence."""


class ResponseAdmissionBlocked(RecoveryBlocked):
    """Response publication lacks final evidence or its exact retained route."""

    def __init__(self):
        super().__init__("response requires final model/death evidence and exact wire route")


class PublicationUncertain(CoordinationError):  # noqa: N818 - nominal outcome name
    """A frozen publishing intent has no authoritative bus-keyed receipt."""


class PublicationActivationBlocked(CoordinationError):  # noqa: N818 - nominal outcome name
    """No bus-owned idempotent append receipt is available in this slice."""
