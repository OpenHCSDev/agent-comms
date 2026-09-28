"""Shared durable coordination errors, independent of record declarations."""


class CoordinationError(RuntimeError):
    """Base class for fail-loud coordinator errors."""


class SchemaVersionError(CoordinationError):
    """The durable store has an unsupported schema version."""


class IntegrityViolationError(CoordinationError):
    """A frozen record or declared relation is inconsistent."""


class IdentityConflict(CoordinationError):  # noqa: N818
    """A requested operation conflicts with frozen coordination identity."""
