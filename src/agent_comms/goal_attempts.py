"""Default-off, durable reservation fence for autonomous goal attempts.

No scheduler uses this module yet. A caller must reserve and claim a launch before
starting Pi, and bind verified goal progress to the claimed attempt before it
can advance a generation. A crash leaves reserved/claimed work unresolved;
ordinary resume cannot turn that state into another model call.

This ledger does not certify a provider response or a registry goal update.
Those witnesses and the ACP/operations integration must be supplied separately.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from uuid import uuid4


class GoalAttemptError(RuntimeError):
    """A goal attempt cannot proceed as requested."""


class StorageUncertainError(GoalAttemptError):
    """A write or durability/readback check failed; never launch from it."""


class ReservationConflictError(GoalAttemptError):
    """This goal/generation is already reserved or is not ready."""


class UnresolvedAttemptError(GoalAttemptError):
    """A blocked or in-flight attempt needs an explicit resolution."""


class StaleAttemptError(GoalAttemptError):
    """A late outcome cannot modify the current goal generation."""


StorageUncertain = StorageUncertainError
ReservationConflict = ReservationConflictError
UnresolvedAttempt = UnresolvedAttemptError
StaleAttempt = StaleAttemptError


@dataclass(frozen=True, slots=True)
class Generation:
    goal_id: str
    number: int
    state: str
    attempt_id: str | None


@dataclass(frozen=True, slots=True)
class Reservation:
    goal_id: str
    generation: int
    attempt_id: str
    token: str


@dataclass(frozen=True, slots=True)
class LaunchPermit:
    reservation: Reservation


_T = TypeVar("_T")


class GoalAttemptStore:
    """Owner-private SQLite authority; no synthetic model or context claims.

    The caller creates the 0700 directory and explicitly initializes it in a
    non-launching setup step. Each change uses BEGIN IMMEDIATE, SQLite's
    synchronous=EXTRA rollback journal, an explicit file/directory fsync, and
    a fresh-connection readback. Failure at any stage returns no permit.
    READY is only launchable with a secret issued after that readback; a visible
    post-COMMIT row whose fsync failed is never authority on its own.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "goal_attempts.sqlite3"
        self._require_root()
        if not self.path.is_file():
            raise StorageUncertain("Goal attempt store is not initialized.")
        self._owned: set[str] = set()
        # These are capabilities, not a replayable mirror of SQLite state.
        # A process restart loses them and requires an explicit user decision.
        self._ready_grants: dict[tuple[str, int], str] = {}
        with closing(self._connect()) as conn:
            try:
                version = conn.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.Error as error:
                raise StorageUncertain("Incomplete goal attempt schema.") from error
            if version != ("2",):
                raise StorageUncertain("Unsupported goal attempt schema.")

    @classmethod
    def initialize(cls, root: str | Path) -> GoalAttemptStore:
        """Explicit setup; creation or fsync uncertainty never returns a store."""
        directory = Path(root)
        if os.name != "posix":
            raise StorageUncertain(
                "Goal attempts require POSIX owner-only directory fsync support."
            )
        if not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise StorageUncertain("An existing owner-0700 directory is required.")
        path = directory / "goal_attempts.sqlite3"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            # Never migrate an earlier READY-without-capability store in place.
            # Its committed rows might have survived a failed writer fsync.
            return cls(directory)
        except OSError as error:
            raise StorageUncertain("Could not create goal attempt store.") from error
        else:
            os.close(fd)
        try:
            with closing(sqlite3.connect(path, timeout=5, isolation_level=None)) as conn:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=EXTRA")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS metadata "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS goals ("
                    "goal_id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation > 0), "
                    "state TEXT NOT NULL CHECK(state IN ('ready','reserved','blocked')), "
                    "attempt_id TEXT, ready_digest TEXT NOT NULL, "
                    "CHECK ((state='ready' AND attempt_id IS NULL AND length(ready_digest)=64) "
                    "OR (state!='ready' AND ready_digest='')))"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS attempts ("
                    "attempt_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, "
                    "generation INTEGER NOT NULL, token TEXT NOT NULL, "
                    "phase TEXT NOT NULL CHECK(phase IN "
                    "('reserved','claimed','failed','succeeded','resolved')), "
                    "progress_witness TEXT, resolution TEXT, "
                    "UNIQUE(goal_id, generation), "
                    "FOREIGN KEY(goal_id) REFERENCES goals(goal_id))"
                )
                conn.execute(
                    "CREATE TABLE human_decisions ("
                    "goal_id TEXT NOT NULL, decision_id TEXT NOT NULL, "
                    "generation INTEGER NOT NULL, "
                    "PRIMARY KEY(goal_id, decision_id), "
                    "FOREIGN KEY(goal_id) REFERENCES goals(goal_id))"
                )
                conn.execute("INSERT INTO metadata(key,value) VALUES('schema_version','2')")
                conn.commit()
            cls._sync_paths(path, directory)
        except (sqlite3.Error, OSError) as error:
            raise StorageUncertain("Goal attempt initialization is uncertain.") from error
        return cls(directory)

    def _require_root(self) -> None:
        if os.name != "posix":
            raise StorageUncertain(
                "Goal attempts require POSIX owner-only directory fsync support."
            )
        if not self.root.is_dir() or stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise StorageUncertain("Goal attempt root must remain owner-0700.")
        if self.path.exists() and stat.S_IMODE(self.path.stat().st_mode) != 0o600:
            raise StorageUncertain("Goal attempt database must remain owner-0600.")

    def _connect(self) -> sqlite3.Connection:
        self._require_root()
        try:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=EXTRA")
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            if mode != ("delete",):
                conn.close()
                raise StorageUncertain("Unexpected SQLite journal mode.")
            return conn
        except sqlite3.Error as error:
            raise StorageUncertain("Goal attempt database unavailable.") from error

    @staticmethod
    def _sync_paths(path: Path, directory: Path) -> None:
        file_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _sync(self) -> None:
        self._sync_paths(self.path, self.root)

    @staticmethod
    def _commit(conn: sqlite3.Connection) -> None:
        conn.commit()

    def _change(
        self, operation: Callable[[sqlite3.Connection], _T], verify: Callable[[_T], bool]
    ) -> _T:
        """A failed commit, sync, or readback is never a launch authorization."""
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = operation(conn)
                self._commit(conn)
                self._sync()
            except GoalAttemptError:
                if conn.in_transaction:
                    conn.rollback()
                raise
            except (sqlite3.Error, OSError) as error:
                if conn.in_transaction:
                    with suppress(sqlite3.Error):
                        conn.rollback()
                raise StorageUncertain("Goal attempt write durability is uncertain.") from error
        try:
            if not verify(result):
                raise StorageUncertain("Goal attempt readback does not attest the write.")
        except sqlite3.Error as error:
            raise StorageUncertain("Goal attempt readback failed.") from error
        return result

    def snapshot(self, goal_id: str) -> Generation | None:
        with closing(self._connect()) as conn:
            try:
                row = conn.execute(
                    "SELECT generation,state,attempt_id FROM goals WHERE goal_id=?", (goal_id,)
                ).fetchone()
            except sqlite3.Error as error:
                raise StorageUncertain("Cannot read goal attempt state.") from error
        return Generation(goal_id, int(row[0]), row[1], row[2]) if row else None

    def _is_generation(self, generation: Generation) -> bool:
        return self.snapshot(generation.goal_id) == generation

    @staticmethod
    def _claim_human_decision(
        conn: sqlite3.Connection, goal_id: str, generation: int, decision_id: str
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO human_decisions(goal_id,decision_id,generation) VALUES(?,?,?)",
                (goal_id, decision_id, generation),
            )
        except sqlite3.IntegrityError as error:
            raise UnresolvedAttempt("That human decision was already used.") from error

    @staticmethod
    def _grant_digest(grant: str) -> str:
        if type(grant) is not str or len(grant) != 64:
            raise UnresolvedAttempt("A valid acknowledged ready grant is required.")
        try:
            raw = bytes.fromhex(grant)
        except ValueError as error:
            raise UnresolvedAttempt("A valid acknowledged ready grant is required.") from error
        if raw.hex() != grant:
            raise UnresolvedAttempt("A valid acknowledged ready grant is required.")
        return hashlib.sha256(raw).hexdigest()

    def _ready_change(
        self, goal_id: str, generation: int, write: Callable[[sqlite3.Connection, str], Generation]
    ) -> Generation:
        # Generate before the transaction, but *publish* the capability only
        # after the complete commit/fsync/readback sequence has returned.
        grant = secrets.token_hex(32)
        digest = self._grant_digest(grant)
        result = self._change(
            lambda conn: write(conn, digest),
            lambda value: self._is_generation(value)
            and self._read_ready_digest(value.goal_id, value.number) == digest,
        )
        self._ready_grants[(goal_id, generation)] = grant
        return result

    def _read_ready_digest(self, goal_id: str, generation: int) -> str | None:
        with closing(self._connect()) as conn:
            try:
                row = conn.execute(
                    "SELECT ready_digest FROM goals "
                    "WHERE goal_id=? AND generation=? AND state='ready'",
                    (goal_id, generation),
                ).fetchone()
            except sqlite3.Error as error:
                raise StorageUncertain("Cannot read ready authority state.") from error
        return row[0] if row else None

    def ready_grant(self, goal_id: str, generation: int) -> str:
        """Delegate only this instance's acknowledged READY authority to a supervisor."""
        grant = self._ready_grants.get((goal_id, generation))
        if grant is None or self._read_ready_digest(goal_id, generation) != self._grant_digest(
            grant
        ):
            raise UnresolvedAttempt("No acknowledged ready grant is available; decide explicitly.")
        return grant

    def _is_attempt(self, attempt: Reservation, phase: str, generation: Generation) -> bool:
        if not self._is_generation(generation):
            return False
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT phase,token FROM attempts "
                "WHERE attempt_id=? AND goal_id=? AND generation=?",
                (attempt.attempt_id, attempt.goal_id, attempt.generation),
            ).fetchone()
        return bool(row == (phase, attempt.token))

    def create_goal(self, goal_id: str) -> Generation:
        """Register one externally created immutable goal ID, initially generation 1."""
        if not goal_id.strip():
            raise ValueError("Goal ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            if conn.execute("SELECT 1 FROM goals WHERE goal_id=?", (goal_id,)).fetchone():
                raise ReservationConflict("Goal ID already has a ledger.")
            conn.execute(
                "INSERT INTO goals(goal_id,generation,state,attempt_id,ready_digest) "
                "VALUES(?,1,'ready',NULL,?)",
                (goal_id, digest),
            )
            return Generation(goal_id, 1, "ready", None)

        return self._ready_change(goal_id, 1, write)

    def reserve(
        self, goal_id: str, expected_generation: int, *, ready_grant: str | None = None
    ) -> Reservation:
        """Reserve once, using acknowledged authority delegated by the goal writer.

        A reopened store never reconstructs a grant from a visible SQLite row.
        It must receive the original writer's grant, or a fresh explicit user
        decision must rotate the READY grant first.
        """
        if ready_grant is None:
            ready_grant = self._ready_grants.get((goal_id, expected_generation))
        if ready_grant is None:
            current = self.snapshot(goal_id)
            if current is None or current.number != expected_generation or current.state != "ready":
                raise ReservationConflict("Goal generation is not ready or already reserved.")
            raise UnresolvedAttempt("No acknowledged ready grant; explicit recovery is required.")
        digest = self._grant_digest(ready_grant)
        attempt = Reservation(goal_id, expected_generation, uuid4().hex, uuid4().hex)

        def write(conn: sqlite3.Connection) -> Reservation:
            row = conn.execute(
                "SELECT generation,state,ready_digest FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            if row is None or row[:2] != (expected_generation, "ready"):
                raise ReservationConflict("Goal generation is not ready or already reserved.")
            if not hmac.compare_digest(row[2], digest):
                raise UnresolvedAttempt("Ready grant is stale or was never acknowledged.")
            conn.execute(
                "INSERT INTO attempts(attempt_id,goal_id,generation,token,phase) "
                "VALUES(?,?,?,?,'reserved')",
                (attempt.attempt_id, goal_id, expected_generation, attempt.token),
            )
            conn.execute(
                "UPDATE goals SET state='reserved',attempt_id=?,ready_digest='' WHERE goal_id=?",
                (attempt.attempt_id, goal_id),
            )
            return attempt

        result = self._change(
            write,
            lambda value: self._is_attempt(
                value,
                "reserved",
                Generation(goal_id, expected_generation, "reserved", value.attempt_id),
            ),
        )
        # A recovered/new store instance cannot claim an old reservation.
        self._ready_grants.pop((goal_id, expected_generation), None)
        self._owned.add(result.attempt_id)
        return result

    def claim_launch(self, reservation: Reservation) -> LaunchPermit:
        """One-shot durable pre-launch claim; only this reserving instance may use it."""
        if reservation.attempt_id not in self._owned:
            raise UnresolvedAttempt("A reservation cannot be replayed after owner loss.")

        def write(conn: sqlite3.Connection) -> LaunchPermit:
            self._require_current(conn, reservation, "reserved")
            conn.execute(
                "UPDATE attempts SET phase='claimed' WHERE attempt_id=?", (reservation.attempt_id,)
            )
            return LaunchPermit(reservation)

        try:
            return self._change(
                write,
                lambda value: self._is_attempt(
                    value.reservation,
                    "claimed",
                    Generation(
                        reservation.goal_id,
                        reservation.generation,
                        "reserved",
                        reservation.attempt_id,
                    ),
                ),
            )
        finally:
            # Even an uncertain claim cannot be used twice in this process.
            self._owned.discard(reservation.attempt_id)

    @staticmethod
    def _require_current(conn: sqlite3.Connection, reservation: Reservation, phase: str) -> None:
        goal = conn.execute(
            "SELECT generation,state,attempt_id FROM goals WHERE goal_id=?", (reservation.goal_id,)
        ).fetchone()
        attempt = conn.execute(
            "SELECT token,phase FROM attempts WHERE attempt_id=? AND goal_id=? AND generation=?",
            (reservation.attempt_id, reservation.goal_id, reservation.generation),
        ).fetchone()
        if goal != (reservation.generation, "reserved", reservation.attempt_id) or attempt != (
            reservation.token,
            phase,
        ):
            raise StaleAttempt("Attempt is no longer current or is already claimed.")

    def record_failed(self, reservation: Reservation, diagnostic: str) -> Generation:
        """Block even when the model outcome is uncertain; never auto-replay."""
        if not diagnostic.strip():
            raise ValueError("Failure diagnostic is required.")

        def write(conn: sqlite3.Connection) -> Generation:
            goal = conn.execute(
                "SELECT generation,state,attempt_id FROM goals WHERE goal_id=?",
                (reservation.goal_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT token,phase FROM attempts "
                "WHERE attempt_id=? AND goal_id=? AND generation=?",
                (reservation.attempt_id, reservation.goal_id, reservation.generation),
            ).fetchone()
            if goal != (reservation.generation, "reserved", reservation.attempt_id) or (
                attempt is None
                or attempt[0] != reservation.token
                or attempt[1] not in {"reserved", "claimed"}
            ):
                raise StaleAttempt("Late failure cannot modify this goal generation.")
            conn.execute(
                "UPDATE attempts SET phase='failed',resolution=? WHERE attempt_id=?",
                (diagnostic, reservation.attempt_id),
            )
            conn.execute(
                "UPDATE goals SET state='blocked',ready_digest='' WHERE goal_id=?",
                (reservation.goal_id,),
            )
            return Generation(
                reservation.goal_id, reservation.generation, "blocked", reservation.attempt_id
            )

        return self._change(
            write,
            lambda value: self._is_attempt(reservation, "failed", value),
        )

    def authorize_abandon_attempt(
        self,
        goal_id: str,
        *,
        expected_generation: int,
        attempt_id: str,
        user_decision_id: str,
    ) -> Generation:
        """A human records an orphaned reservation/claim as uncertain and blocked.

        A claimed attempt may have reached a provider: this does not cancel a
        request or authorize replay. Another explicit decision is necessary
        before `authorize_retry` can create a new READY generation.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user abandonment decision ID is required.")

        def write(conn: sqlite3.Connection) -> Generation:
            goal = conn.execute(
                "SELECT generation,state,attempt_id FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            attempt = conn.execute(
                "SELECT phase FROM attempts WHERE attempt_id=? AND goal_id=? AND generation=?",
                (attempt_id, goal_id, expected_generation),
            ).fetchone()
            if goal != (expected_generation, "reserved", attempt_id) or attempt not in {
                ("reserved",),
                ("claimed",),
            }:
                raise StaleAttempt("Attempt changed before human abandonment decision.")
            self._claim_human_decision(conn, goal_id, expected_generation, user_decision_id)
            conn.execute(
                "UPDATE attempts SET phase='failed',resolution=? WHERE attempt_id=?",
                (user_decision_id, attempt_id),
            )
            conn.execute(
                "UPDATE goals SET state='blocked',ready_digest='' WHERE goal_id=?", (goal_id,)
            )
            return Generation(goal_id, expected_generation, "blocked", attempt_id)

        return self._change(
            write,
            lambda value: self.snapshot(value.goal_id) == value
            and self._is_attempt_phase(attempt_id, "failed", user_decision_id),
        )

    def _is_attempt_phase(self, attempt_id: str, phase: str, resolution: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT phase,resolution FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return bool(row == (phase, resolution))

    def resume(self, goal_id: str, expected_generation: int) -> Generation:
        """Ordinary same-ID resume never resolves a reserved or blocked attempt."""
        current = self.snapshot(goal_id)
        if current is None or current.number != expected_generation:
            raise StaleAttempt("Goal generation changed before resume.")
        if current.state != "ready" or current.attempt_id is not None:
            raise UnresolvedAttempt("Explicit attempt resolution is required before resume.")
        self.ready_grant(goal_id, expected_generation)
        return current

    def authorize_ready_recovery(
        self, goal_id: str, *, expected_generation: int, user_decision_id: str
    ) -> Generation:
        """A human decision rotates lost READY authority after a restart/uncertain write.

        This is not an automatic retry: integration must present the durable
        goal and verify the *real* user decision and registry goal identity.
        If a reservation raced this decision, neither its grant nor its attempt
        is overwritten. Reusing a decision ID cannot mint another grant.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user recovery decision ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            row = conn.execute(
                "SELECT generation,state,attempt_id FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            if row != (expected_generation, "ready", None):
                raise UnresolvedAttempt("Goal is not ready; an attempt may already be unresolved.")
            self._claim_human_decision(conn, goal_id, expected_generation, user_decision_id)
            conn.execute(
                "UPDATE goals SET generation=generation+1,ready_digest=? WHERE goal_id=?",
                (digest, goal_id),
            )
            return Generation(goal_id, expected_generation + 1, "ready", None)

        return self._ready_change(goal_id, expected_generation + 1, write)

    def authorize_retry(
        self,
        goal_id: str,
        *,
        expected_generation: int,
        attempt_id: str,
        user_decision_id: str,
    ) -> Generation:
        """A separate explicit user retry decision retires uncertainty; resume alone cannot.

        This records an authorization, not a provider receipt. Integration must
        route only a real user action here; same-UID user authentication is not
        claimed by the ledger.
        """
        if not user_decision_id.strip():
            raise ValueError("An explicit user retry decision ID is required.")

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            current = conn.execute(
                "SELECT generation,state,attempt_id FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            if current != (expected_generation, "blocked", attempt_id):
                raise UnresolvedAttempt("Blocked attempt changed or is not resolved.")
            row = conn.execute(
                "SELECT phase FROM attempts WHERE attempt_id=? AND goal_id=? AND generation=?",
                (attempt_id, goal_id, expected_generation),
            ).fetchone()
            if row != ("failed",):
                raise UnresolvedAttempt(
                    "Only a blocked, recorded failure may be retried explicitly."
                )
            self._claim_human_decision(conn, goal_id, expected_generation, user_decision_id)
            conn.execute(
                "UPDATE attempts SET phase='resolved',resolution=? WHERE attempt_id=?",
                (user_decision_id, attempt_id),
            )
            conn.execute(
                "UPDATE goals SET generation=generation+1,state='ready',attempt_id=NULL, "
                "ready_digest=? WHERE goal_id=?",
                (digest, goal_id),
            )
            return Generation(goal_id, expected_generation + 1, "ready", None)

        return self._ready_change(goal_id, expected_generation + 1, write)

    def record_verified_progress(self, permit: LaunchPermit, progress_witness: str) -> Generation:
        """Advance only from a claimed launch and caller-verified progress witness.

        The witness must be bound to an actual registry progress update by the
        integration owner. This primitive alone cannot verify that other file.
        """
        if not progress_witness.strip():
            raise ValueError("A nonempty verified progress witness is required.")
        reservation = permit.reservation

        def write(conn: sqlite3.Connection, digest: str) -> Generation:
            self._require_current(conn, reservation, "claimed")
            conn.execute(
                "UPDATE attempts SET phase='succeeded',progress_witness=? WHERE attempt_id=?",
                (progress_witness, reservation.attempt_id),
            )
            conn.execute(
                "UPDATE goals SET generation=generation+1,state='ready',attempt_id=NULL, "
                "ready_digest=? WHERE goal_id=?",
                (digest, reservation.goal_id),
            )
            return Generation(reservation.goal_id, reservation.generation + 1, "ready", None)

        return self._ready_change(reservation.goal_id, reservation.generation + 1, write)
