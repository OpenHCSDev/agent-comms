"""Coordination session: owned coordinator state and transitions."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import (
    IntegrityViolationError,
)


class CoordinationSession(CoordinationStore):
    def __init__(
        self, path: str, *, clock_ms: Callable[[], int] | None = None, lock_timeout: float = 5.0
    ) -> None:
        super().__init__(path, lock_timeout=lock_timeout)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def now(self, floor: int = 0) -> int:
        now = self._clock_ms()
        if type(now) is not int or now < 0:
            raise ValueError("coordinator clock must return a nonnegative integer millisecond")
        return max(now, floor)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._transaction_lifetime() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @contextmanager
    def irreversible_admission(self) -> Iterator[sqlite3.Connection]:
        """Exclude readers before external effects, never upgrade after sending.

        In rollback-journal mode BEGIN IMMEDIATE admits readers that can block
        COMMIT. The raw native writer must acquire the exclusive lock before any
        prompt byte; with its zero timeout, contention remains pre-admission.
        """
        with self._transaction_lifetime() as db:
            db.execute("BEGIN EXCLUSIVE")
            yield db

    @contextmanager
    def _transaction_lifetime(self) -> Iterator[sqlite3.Connection]:
        """Shared non-nesting, commit and rollback ownership for write scopes."""
        db = self._connection
        if db.in_transaction:
            raise IntegrityViolationError("nested coordinator transaction is not allowed")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

    @contextmanager
    def read(self) -> Iterator[None]:
        """Project one committed database state, or reuse an enclosing write transaction."""
        db = self._connection
        if db.in_transaction:
            yield
            return
        db.execute("BEGIN")
        try:
            yield
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")
