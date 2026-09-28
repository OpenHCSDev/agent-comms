"""Opt-in, append-writer-maintained private bus prefix certificate.

The JSONL bus remains authoritative. Only the canonical private WireLog writer may
advance this certificate, after bus and directory fsync under the bus lock. The
fsynced bus marker independently seals the SQLite inode revision and certificate;
SQL index contents are never trusted on bus revision alone. A pending writer
intent forces a complete canonical rebuild; a changed bus revision with a final,
unchanged sidecar requires full prefix hashing before suffix adoption. Unexpected
sidecar changes deny, not silently rebuild. This is not native input or an ACK.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .checkpoint_seals import FinalSeal, PendingSeal, PrefixSeal, file_revision
from .field_codec import FieldCodec
from .wire_metadata import WireMetadata

if TYPE_CHECKING:
    from .bus_publication import CommittedInitial
    from .messages import Message
    from .wire_log import WireLog

_SEED = hashlib.sha256(b"agent-comms:private-bus-prefix:v1\0").digest()
_TAIL_BYTES = 4096


@dataclass(frozen=True)
class PrefixWitness(PrefixSeal):
    # The high-water is a derived page fact, not a persistent seal field.
    latest_initial_seq: int = field(default=0, compare=False, metadata={"seal_exclude": True})

    @property
    def device(self) -> int:
        return self.revision[0]

    @property
    def inode(self) -> int:
        return self.revision[1]

    @property
    def offset(self) -> int:
        return self.revision[2]

    def seal(self) -> PrefixSeal:
        return FieldCodec.decode(PrefixSeal, FieldCodec.project(self, "seal"))


@dataclass(frozen=True)
class PrefixCertificate:
    """The existing SQLite row, decoded once at its persistence boundary."""

    singleton: Literal[1] = field(metadata={"sqlite_constraint": "PRIMARY KEY CHECK(singleton=1)"})
    version: Literal[1]
    root_id: str
    device: int
    inode: int
    offset: int
    through_seq: int
    digest: str
    tail: str
    mtime_ns: int
    ctime_ns: int

    def witness(self) -> PrefixWitness:
        return PrefixWitness(
            self.root_id,
            (self.device, self.inode, self.offset, self.mtime_ns, self.ctime_ns),
            self.through_seq,
            self.digest,
            self.tail,
        )

    @classmethod
    def capture(
        cls, root_id: str, info: os.stat_result, seq: int, digest: bytes, tail: str
    ) -> PrefixCertificate:
        return cls(
            1,
            1,
            root_id,
            info.st_dev,
            info.st_ino,
            info.st_size,
            seq,
            digest.hex(),
            tail,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @classmethod
    def create_table(cls, db: sqlite3.Connection) -> None:
        kinds = FieldCodec._types(cls)
        columns = [
            f"{item.name} {'TEXT' if kinds[item.name] is str else 'INTEGER'} "
            + item.metadata.get("sqlite_constraint", "NOT NULL")
            for item in fields(cls)
        ]
        db.execute(f"CREATE TABLE certificate({','.join(columns)})")

    def insert(self, db: sqlite3.Connection) -> None:
        values = FieldCodec.encode(self)
        db.execute(
            f"INSERT INTO certificate ({','.join(values)}) "
            f"VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def update(self, db: sqlite3.Connection) -> None:
        values = FieldCodec.encode(self)
        db.execute(
            f"UPDATE certificate SET {','.join(f'{name}=?' for name in values)} WHERE singleton=1",
            tuple(values.values()),
        )


def _failure(message: str) -> Exception:
    from .errors import RelationViolationError

    return RelationViolationError(message)


def _path(bus_path: Path) -> Path:
    return bus_path.with_name("private_bus_checkpoint.sqlite3")


def _tail(stream, offset: int) -> str:
    stream.seek(max(0, offset - _TAIL_BYTES))
    return hashlib.sha256(stream.read(min(_TAIL_BYTES, offset))).hexdigest()


def _chain(previous: bytes, raw: bytes) -> bytes:
    return hashlib.sha256(previous + len(raw).to_bytes(8, "big") + raw).digest()


def _directory_sync(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    from .errors import RelationViolationError

    if path.is_symlink() or not path.exists():
        raise RelationViolationError("Private bus checkpoint is missing or redirected.")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise RelationViolationError("Private bus checkpoint is not owner-only.")
    uri = f"{path.absolute().as_uri()}?mode={'ro' if readonly else 'rw'}"
    db = sqlite3.connect(uri, uri=True, timeout=0.05)
    db.row_factory = sqlite3.Row
    if readonly:
        db.execute("PRAGMA query_only=ON")
    else:
        db.execute("PRAGMA synchronous=FULL")
    return db


def _saved(db: sqlite3.Connection) -> PrefixWitness:
    tables = {item[0] for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables != {"certificate", "response_keys", "initials", "addressed"}:
        raise _failure("Private bus checkpoint schema is unavailable.")
    row = db.execute("SELECT * FROM certificate WHERE singleton=1").fetchone()
    if row is None:
        raise _failure("Private bus checkpoint schema is unavailable.")
    try:
        return FieldCodec.decode(PrefixCertificate, dict(row)).witness()
    except (TypeError, ValueError) as error:
        raise _failure("Private bus checkpoint identity is malformed.") from error


def _index_row(
    db: sqlite3.Connection,
    offset: int,
    raw: bytes,
    message: Message,
    receipt: Mapping[str, object] | None,
    initial: CommittedInitial | None,
) -> None:
    if receipt is None and initial is None and message.claim_transition is None:
        raise _failure("Unattested public initial cannot enter a certified private root.")
    if receipt is not None:
        db.execute("INSERT INTO response_keys(key) VALUES (?)", (receipt["publication_key"],))
    if initial is not None:
        db.execute(
            "INSERT INTO initials(seq,message_id,offset,length) VALUES(?,?,?,?)",
            (message.seq, message.message_id, offset, len(raw)),
        )
        for recipient in initial.audience.recipients:
            db.execute(
                "INSERT INTO addressed(lookup,seq) VALUES(?,?)",
                (recipient.recipient_lookup, message.seq),
            )


def install_private_bus_checkpoint(bus: WireLog) -> PrefixWitness:
    """Explicitly certify the complete current private bus without rewriting it.

    The canonical writer lock excludes appenders throughout the one-time scan.
    Build the existing certificate/index schema off-path, then publish its inode
    and durable marker binding. Before publication a failure leaves no active
    sidecar; after publication an uncertain marker write retains the sidecar and
    existing read barrier denies access rather than trusting an unsealed index.
    """
    from .wire_log import WireLog

    if type(bus) is not WireLog or bus.path.name != "bus.jsonl":
        raise TypeError("Canonical private WireLog required")
    with bus.locked():
        marker = bus._private_marker_unlocked()
        if not marker.claims:
            raise _failure("Checkpoint installation needs a claim-enabled private root.")
        path = _path(bus.path)
        if path.exists() or path.is_symlink() or marker.checkpoint_seal is not None:
            raise _failure("Private bus checkpoint is already installed.")
        if not bus.path.exists():
            if marker.last_seq != 0:
                raise _failure("Private bus is missing its reserved publication.")
            fd = os.open(
                bus.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _directory_sync(bus.path)
        # Staging beside the destination makes publication atomic and never
        # exposes a partially built index to current readers or appenders.
        with tempfile.TemporaryDirectory(prefix=".checkpoint-install-", dir=path.parent) as stage:
            staged = Path(stage) / path.name
            fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            with closing(_connect(staged)) as db:
                PrefixCertificate.create_table(db)
                db.executescript(
                    "CREATE TABLE response_keys(key TEXT PRIMARY KEY);"
                    "CREATE TABLE initials(seq INTEGER PRIMARY KEY,message_id TEXT NOT NULL,"
                    "offset INTEGER NOT NULL,length INTEGER NOT NULL);"
                    "CREATE TABLE addressed(lookup TEXT NOT NULL,seq INTEGER NOT NULL,"
                    "PRIMARY KEY(lookup,seq));"
                )
                with bus.path.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    digest = _SEED
                    through_seq = 0

                    def collect(offset, raw, message, receipt, initial):
                        nonlocal digest, through_seq
                        digest = _chain(digest, raw)
                        through_seq = message.seq
                        _index_row(db, offset, raw, message, receipt, initial)

                    with db:
                        for _ in bus._verified_private_rows_unlocked(marker, on_row=collect):
                            pass
                        if through_seq != marker.last_seq:
                            raise _failure("Private bus has an unsettled publication sequence.")
                        if file_revision(os.fstat(stream.fileno())) != file_revision(
                            info
                        ) or file_revision(bus.path.stat()) != file_revision(info):
                            raise _failure("Private bus changed during checkpoint installation.")
                        PrefixCertificate.capture(
                            marker.root_id, info, through_seq, digest, _tail(stream, info.st_size)
                        ).insert(db)
            _directory_sync(staged)
            with closing(_connect(staged, readonly=True)) as db:
                witness = _saved(db)
            os.replace(staged, path)
            _directory_sync(path)
            marker.seal_with(FinalSeal.capture(witness, path))
            bus.write_metadata_unlocked(marker)
            return witness


def certificate_enabled(bus_path: Path) -> bool:
    path = _path(bus_path)
    return path.exists() or path.is_symlink()


def _recover_pending_unlocked(
    bus: WireLog,
    marker: WireMetadata,
    db: sqlite3.Connection,
    db_path: Path,
    info: os.stat_result,
    seal: PendingSeal,
) -> PrefixWitness:
    """Repair only a durable writer intent after a COMPLETE canonical bus scan."""
    prior = seal.prior
    digest = _SEED
    prior_seen = prior.revision[2] == 0
    prior_seq = 0
    last_seq = 0
    count = 0
    size = 0

    with db:
        db.execute("DELETE FROM addressed")
        db.execute("DELETE FROM initials")
        db.execute("DELETE FROM response_keys")

        def collect(offset, raw, message, receipt, initial):
            nonlocal digest, prior_seen, prior_seq, last_seq, count, size
            count += 1
            size += len(raw)
            if count > 100_000 or size > 128 * 1024 * 1024:
                raise _failure("Private bus pending recovery exceeds cold bound.")
            digest = _chain(digest, raw)
            if offset + len(raw) == prior.revision[2]:
                prior_seen = True
                prior_seq = message.seq
                if digest.hex() != prior.digest or prior_seq != prior.through_seq:
                    raise _failure("Private bus checkpoint old prefix changed during recovery.")
            last_seq = message.seq
            _index_row(db, offset, raw, message, receipt, initial)

        for _ in bus._verified_private_rows_unlocked(marker, on_row=collect):
            pass
        if not prior_seen or (prior.revision[2] == 0 and prior.digest != _SEED.hex()):
            raise _failure("Private bus checkpoint old prefix is unavailable.")
        if file_revision(bus.path.stat()) != file_revision(info):
            raise _failure("Private bus changed during pending recovery.")
        with bus.path.open("rb") as stream:
            expected = PrefixWitness(
                marker.root_id,
                file_revision(info),
                last_seq,
                digest.hex(),
                _tail(stream, info.st_size),
            )
        if expected.seal() != seal.expected or last_seq > marker.last_seq:
            raise _failure("Private bus checkpoint pending suffix differs from intent.")
        PrefixCertificate.capture(expected.root_id, info, last_seq, digest, expected.tail).update(
            db
        )
    _directory_sync(db_path)
    marker.seal_with(FinalSeal.capture(expected, db_path))
    bus.write_metadata_unlocked(marker)
    return expected


def verify_private_bus_checkpoint_unlocked(bus: WireLog, marker: WireMetadata) -> PrefixWitness:
    """Check an exact revision or cold-validate every old byte on a changed revision.

    Caller holds the bus lock and has fsynced the bus inode and directory.
    """
    path = _path(bus.path)
    try:
        with closing(_connect(path)) as db, bus.path.open("rb") as stream:
            saved = _saved(db)
            info = os.fstat(stream.fileno())
            if (
                saved.root_id != marker.root_id
                or (saved.device, saved.inode) != (info.st_dev, info.st_ino)
                or info.st_size < saved.offset
            ):
                raise _failure("Private bus checkpoint root/inode/size changed.")
            recovered = marker.seal.recover(bus, marker, db, path, saved, info)
            if recovered is not None:
                return recovered
            if _tail(stream, saved.offset) != saved.tail:
                raise _failure("Private bus checkpoint prefix tail changed.")
            if saved.through_seq > marker.last_seq:
                raise _failure("Private bus checkpoint exceeds the durable sequence marker.")
            if file_revision(info) == saved.revision:
                return saved
            # Changed revision: a suffix alone cannot rule out an earlier in-place edit.
            # Validate complete canonical history, compare digest at the saved offset,
            # and collect only newly appended rows for one atomic index transaction.
            digest = _SEED
            observed_prefix = saved.offset == 0
            prefix_seq = 0
            additions: list[
                tuple[int, bytes, Message, Mapping[str, object] | None, CommittedInitial | None]
            ] = []
            suffix_bytes = 0

            def collect(
                offset: int,
                raw: bytes,
                message: Message,
                receipt: Mapping[str, object] | None,
                initial: CommittedInitial | None,
            ) -> None:
                nonlocal digest, observed_prefix, prefix_seq, suffix_bytes
                if receipt is None and initial is None and message.claim_transition is None:
                    raise _failure("Unattested public initial blocks certified prefix.")
                digest = _chain(digest, raw)
                end = offset + len(raw)
                if end == saved.offset:
                    observed_prefix = True
                    prefix_seq = message.seq
                    if digest.hex() != saved.digest or prefix_seq != saved.through_seq:
                        raise _failure("Private bus checkpoint certified prefix changed.")
                elif end > saved.offset:
                    if not observed_prefix or offset < saved.offset:
                        raise _failure("Private bus checkpoint offset is not a row boundary.")
                    suffix_bytes += len(raw)
                    if suffix_bytes > 16 * 1024 * 1024 or len(additions) >= 10_000:
                        raise _failure(
                            "Private bus checkpoint crash suffix exceeds recovery bound."
                        )
                    additions.append((offset, raw, message, receipt, initial))

            for _ in bus._verified_private_rows_unlocked(marker, on_row=collect):
                pass
            if not observed_prefix or (saved.offset == 0 and saved.digest != _SEED.hex()):
                raise _failure("Private bus checkpoint prefix is unavailable.")
            if file_revision(os.fstat(stream.fileno())) != file_revision(info) or file_revision(
                bus.path.stat()
            ) != file_revision(info):
                raise _failure("Private bus changed during checkpoint validation.")
            # Full parser above checked all cross-prefix response key duplicates.
            last_seq = additions[-1][2].seq if additions else saved.through_seq
            expected = PrefixWitness(
                saved.root_id,
                file_revision(info),
                last_seq,
                digest.hex(),
                _tail(stream, info.st_size),
            )
            marker.seal_with(PendingSeal.capture(saved, expected, path))
            bus.write_metadata_unlocked(marker)
            with db:
                for offset, raw, message, receipt, initial in additions:
                    _index_row(db, offset, raw, message, receipt, initial)
                PrefixCertificate.capture(
                    saved.root_id, info, last_seq, digest, expected.tail
                ).update(db)
            _directory_sync(path)
            marker.seal_with(FinalSeal.capture(expected, path))
            bus.write_metadata_unlocked(marker)
            return _saved(db)
    except (sqlite3.Error, OSError) as error:
        raise _failure("Private bus checkpoint verification is unavailable.") from error


def append_private_bus_checkpoint_unlocked(
    bus: WireLog,
    marker: WireMetadata,
    raw: bytes,
    message: Message,
    receipt: Mapping[str, object] | None,
    initial: CommittedInitial | None,
) -> PrefixWitness:
    """Append one certified row only AFTER the canonical bus/parent fsync."""
    path = _path(bus.path)
    try:
        with closing(_connect(path)) as db, bus.path.open("rb") as stream:
            saved = _saved(db)
            info = os.fstat(stream.fileno())
            offset = info.st_size - len(raw)
            if (
                saved.root_id != marker.root_id
                or (saved.device, saved.inode) != (info.st_dev, info.st_ino)
                or offset != saved.offset
                or message.seq <= saved.through_seq
            ):
                raise _failure("Private bus checkpoint append lost its prefix fence.")
            marker.seal.check_final(saved, path)
            stream.seek(offset)
            if stream.read(len(raw)) != raw or _tail(stream, offset) != saved.tail:
                raise _failure("Private bus checkpoint append bytes differ.")
            digest = _chain(bytes.fromhex(saved.digest), raw)
            expected = PrefixWitness(
                saved.root_id,
                file_revision(info),
                message.seq,
                digest.hex(),
                _tail(stream, info.st_size),
            )
            marker.seal_with(PendingSeal.capture(saved, expected, path))
            bus.write_metadata_unlocked(marker)
            with db:
                _index_row(db, offset, raw, message, receipt, initial)
                PrefixCertificate.capture(
                    saved.root_id, info, message.seq, digest, expected.tail
                ).update(db)
            _directory_sync(path)
            marker.seal_with(FinalSeal.capture(expected, path))
            bus.write_metadata_unlocked(marker)
            return _saved(db)
    except (sqlite3.Error, OSError) as error:
        raise _failure("Private bus checkpoint append outcome UNKNOWN.") from error


def certified_initial_page_unlocked(
    bus: WireLog,
    marker: WireMetadata,
    lookup: str,
    *,
    after: int = 0,
    limit: int = 100,
) -> tuple[PrefixWitness, tuple[CommittedInitial, ...], bool]:
    """Complete addressed page; caller holds bus lock and must bind SQL/native proof.

    The returned through sequence is a global bus bound. The page witness's
    latest_initial_seq is derived from this same sealed index connection under
    the final revision fence; only that field bounds initial-source coverage.
    Neither bound grants native-input acceptance, ACK, or skip permission.
    Recheck the witness under the same bus-held SQL commit; an unlocked return
    is advisory only.
    """
    from .bus_publication import PRIVATE_WIRE_FIELD, unique_wire_object, validate_initial_record
    from .errors import RelationViolationError

    if (
        type(lookup) is not str
        or len(lookup) != 32
        or any(c not in "0123456789abcdef" for c in lookup)
        or type(after) is not int
        or after < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("Exact bounded recipient page required")
    witness = verify_private_bus_checkpoint_unlocked(bus, marker)
    try:
        with (
            closing(_connect(_path(bus.path), readonly=True)) as db,
            bus.path.open("rb") as stream,
        ):
            rows = db.execute(
                "SELECT i.seq,i.message_id,i.offset,i.length FROM addressed a "
                "JOIN initials i ON i.seq=a.seq "
                "WHERE a.lookup=? AND i.seq>? ORDER BY i.seq LIMIT ?",
                (lookup, after, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            latest_initial_seq = db.execute("SELECT MAX(seq) FROM initials").fetchone()[0]
            if latest_initial_seq is None:
                latest_initial_seq = 0
            if (
                type(latest_initial_seq) is not int
                or latest_initial_seq < 0
                or latest_initial_seq > witness.through_seq
            ):
                raise _failure("Certified initial high-water is invalid.")
            initials = []
            for row in rows[:limit]:
                stream.seek(row["offset"])
                raw = stream.read(row["length"])
                if len(raw) != row["length"] or not raw.endswith(b"\n"):
                    raise _failure("Certified initial row changed.")
                record = json.loads(raw, object_pairs_hook=unique_wire_object)
                if not isinstance(record, dict) or PRIVATE_WIRE_FIELD not in record:
                    raise _failure("Certified initial row is unavailable.")
                initial = validate_initial_record(record, witness.root_id)
                if (
                    initial.message.seq != row["seq"]
                    or initial.message.message_id != row["message_id"]
                    or not any(r.recipient_lookup == lookup for r in initial.audience.recipients)
                ):
                    raise _failure("Certified initial lookup differs from bus row.")
                initials.append(initial)
            if (
                file_revision(_path(bus.path).stat()) != marker.seal.db_revision
                or bus._private_marker_unlocked().seal != marker.seal
                or file_revision(bus.path.stat()) != witness.revision
            ):
                raise _failure("Certified page changed during its read fence.")
            return (
                replace(witness, latest_initial_seq=latest_initial_seq),
                tuple(initials),
                has_more,
            )
    except RelationViolationError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise _failure("Certified initial page is unavailable.") from error
