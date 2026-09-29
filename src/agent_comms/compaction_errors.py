"""Durability failure domains shared by journal records and transaction owners."""


class CompactionJournalError(RuntimeError):
    """Journal state cannot authorize another dispatch; fail closed."""


class CompactionJournalUnknownError(CompactionJournalError):
    """A COMMIT may be visible but lacks durable confirmation; never dispatch/replay."""
