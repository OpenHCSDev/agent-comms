"""Append-writer-maintained private bus prefix certificate.

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
from contextlib import closing, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .checkpoint_seals import FinalSeal, PendingSeal, PrefixSeal, file_revision
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .typed_table import Column, SQLiteSchemaObject, TypedTable
from .wire_metadata import WireMetadata
from .wire_record import WireRecord

if TYPE_CHECKING:
    from .bus_publication import CommittedDelivery
    from .wire_log import WireLog

_SEED = hashlib.sha256(b"agent-comms:private-bus-prefix:v1\0").digest()
_TAIL_BYTES = 4096


@dataclass(frozen=True)
class PrefixWitness(PrefixSeal):
    # The high-water is a derived page fact, not a persistent seal field.
    latest_source_seq: int = field(default=0, compare=False, metadata={"seal_exclude": True})

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


class CheckpointTable:
    """Source-derived rows bound by the canonical writer's durable seal."""


@dataclass(frozen=True)
class ResponseKeys(CheckpointTable, TypedTable):
    key: str = field(metadata={"sql": Column(primary_key=True)})


@dataclass(frozen=True)
class DeliverySources(CheckpointTable, TypedTable):
    seq: int = field(metadata={"sql": Column(primary_key=True, check="seq>0")})
    message_id: str
    offset: int = field(metadata={"sql": Column(check="offset>=0")})
    length: int = field(metadata={"sql": Column(check="length>0")})


@dataclass(frozen=True)
class Addressed(CheckpointTable, TypedTable):
    lookup: str = field(metadata={"sql": Column(primary_key=True)})
    seq: int = field(metadata={"sql": Column(True, references=(DeliverySources, "seq"))})


@dataclass(frozen=True)
class PrefixCertificate(CheckpointTable, TypedTable):
    """The existing SQLite row, decoded once at its persistence boundary."""

    singleton: Literal[1] = field(metadata={"sql": Column(primary_key=True)})
    version: Literal[2]
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
            2,
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
    schema = {
        name: sql
        for table in TypedTable.members_with(CheckpointTable)
        for name, sql in table.schema_objects().items()
    }
    actual = SQLiteSchemaObject.read(
        db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
    )
    if {row.name: row.sql for row in actual} != schema:
        raise RelationViolationError("Private bus checkpoint schema is unavailable.")
    try:
        row = PrefixCertificate.one(db, singleton=1)
        if row is None:
            raise RelationViolationError("Private bus checkpoint certificate is missing.")
        return row.witness()
    except (TypeError, ValueError) as error:
        raise RelationViolationError("Private bus checkpoint identity is malformed.") from error


def _index_row(db: sqlite3.Connection, offset: int, raw: bytes, record: WireRecord) -> None:
    for row in record.checkpoint_rows(offset, len(raw)):
        row.insert(db)


def install_private_bus_checkpoint(bus: WireLog, *, _bus_locked: bool = False) -> PrefixWitness:
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
    with nullcontext() if _bus_locked else bus.locked():
        marker = bus._private_marker_unlocked()
        if not marker.claims:
            raise RelationViolationError(
                "Checkpoint installation needs a claim-enabled private root."
            )
        path = _path(bus.path)
        if path.exists() or path.is_symlink() or marker.checkpoint_seal is not None:
            raise RelationViolationError("Private bus checkpoint is already installed.")
        if not bus.path.exists():
            if marker.last_seq != 0:
                raise RelationViolationError("Private bus is missing its reserved publication.")
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
                for table in TypedTable.members_with(CheckpointTable):
                    table.create(db)
                with bus.path.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    digest = _SEED
                    through_seq = 0

                    def collect(offset, raw, record):
                        nonlocal digest, through_seq
                        digest = _chain(digest, raw)
                        through_seq = record.message.seq
                        _index_row(db, offset, raw, record)

                    with db:
                        for _ in bus.verified_records_unlocked(marker, on_row=collect):
                            pass
                        if through_seq != marker.last_seq:
                            raise RelationViolationError(
                                "Private bus has an unsettled publication sequence."
                            )
                        if file_revision(os.fstat(stream.fileno())) != file_revision(
                            info
                        ) or file_revision(bus.path.stat()) != file_revision(info):
                            raise RelationViolationError(
                                "Private bus changed during checkpoint installation."
                            )
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
        db.execute(f"DELETE FROM {DeliverySources.declared_name}")
        db.execute("DELETE FROM response_keys")

        def collect(offset, raw, record):
            nonlocal digest, prior_seen, prior_seq, last_seq, count, size
            count += 1
            size += len(raw)
            if count > 100_000 or size > 128 * 1024 * 1024:
                raise RelationViolationError("Private bus pending recovery exceeds cold bound.")
            digest = _chain(digest, raw)
            if offset + len(raw) == prior.revision[2]:
                prior_seen = True
                prior_seq = record.message.seq
                if digest.hex() != prior.digest or prior_seq != prior.through_seq:
                    raise RelationViolationError(
                        "Private bus checkpoint old prefix changed during recovery."
                    )
            last_seq = record.message.seq
            _index_row(db, offset, raw, record)

        for _ in bus.verified_records_unlocked(marker, on_row=collect):
            pass
        if not prior_seen or (prior.revision[2] == 0 and prior.digest != _SEED.hex()):
            raise RelationViolationError("Private bus checkpoint old prefix is unavailable.")
        if file_revision(bus.path.stat()) != file_revision(info):
            raise RelationViolationError("Private bus changed during pending recovery.")
        with bus.path.open("rb") as stream:
            expected = PrefixWitness(
                marker.root_id,
                file_revision(info),
                last_seq,
                digest.hex(),
                _tail(stream, info.st_size),
            )
        if expected.seal() != seal.expected or last_seq > marker.last_seq:
            raise RelationViolationError(
                "Private bus checkpoint pending suffix differs from intent."
            )
        PrefixCertificate.capture(expected.root_id, info, last_seq, digest, expected.tail).upsert(
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
                raise RelationViolationError("Private bus checkpoint root/inode/size changed.")
            recovered = marker.seal.recover(bus, marker, db, path, saved, info)
            if recovered is not None:
                return recovered
            if _tail(stream, saved.offset) != saved.tail:
                raise RelationViolationError("Private bus checkpoint prefix tail changed.")
            if saved.through_seq > marker.last_seq:
                raise RelationViolationError(
                    "Private bus checkpoint exceeds the durable sequence marker."
                )
            if file_revision(info) == saved.revision:
                return saved
            # Changed revision: a suffix alone cannot rule out an earlier in-place edit.
            # Validate complete canonical history, compare digest at the saved offset,
            # and collect only newly appended rows for one atomic index transaction.
            digest = _SEED
            observed_prefix = saved.offset == 0
            prefix_seq = 0
            additions: list[tuple[int, bytes, WireRecord]] = []
            suffix_bytes = 0

            def collect(
                offset: int,
                raw: bytes,
                record: WireRecord,
            ) -> None:
                nonlocal digest, observed_prefix, prefix_seq, suffix_bytes
                digest = _chain(digest, raw)
                end = offset + len(raw)
                if end == saved.offset:
                    observed_prefix = True
                    prefix_seq = record.message.seq
                    if digest.hex() != saved.digest or prefix_seq != saved.through_seq:
                        raise RelationViolationError(
                            "Private bus checkpoint certified prefix changed."
                        )
                elif end > saved.offset:
                    if not observed_prefix or offset < saved.offset:
                        raise RelationViolationError(
                            "Private bus checkpoint offset is not a row boundary."
                        )
                    suffix_bytes += len(raw)
                    if suffix_bytes > 16 * 1024 * 1024 or len(additions) >= 10_000:
                        raise RelationViolationError(
                            "Private bus checkpoint crash suffix exceeds recovery bound."
                        )
                    additions.append((offset, raw, record))

            for _ in bus.verified_records_unlocked(marker, on_row=collect):
                pass
            if not observed_prefix or (saved.offset == 0 and saved.digest != _SEED.hex()):
                raise RelationViolationError("Private bus checkpoint prefix is unavailable.")
            if file_revision(os.fstat(stream.fileno())) != file_revision(info) or file_revision(
                bus.path.stat()
            ) != file_revision(info):
                raise RelationViolationError("Private bus changed during checkpoint validation.")
            # Full parser above checked all cross-prefix response key duplicates.
            last_seq = additions[-1][2].message.seq if additions else saved.through_seq
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
                for offset, raw, record in additions:
                    _index_row(db, offset, raw, record)
                PrefixCertificate.capture(
                    saved.root_id, info, last_seq, digest, expected.tail
                ).upsert(db)
            _directory_sync(path)
            marker.seal_with(FinalSeal.capture(expected, path))
            bus.write_metadata_unlocked(marker)
            return _saved(db)
    except (sqlite3.Error, OSError) as error:
        raise RelationViolationError(
            "Private bus checkpoint verification is unavailable."
        ) from error


def append_private_bus_checkpoint_unlocked(
    bus: WireLog,
    marker: WireMetadata,
    raw: bytes,
    record: WireRecord,
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
                or record.message.seq <= saved.through_seq
            ):
                raise RelationViolationError("Private bus checkpoint append lost its prefix fence.")
            marker.seal.check_final(saved, path)
            stream.seek(offset)
            if stream.read(len(raw)) != raw or _tail(stream, offset) != saved.tail:
                raise RelationViolationError("Private bus checkpoint append bytes differ.")
            digest = _chain(bytes.fromhex(saved.digest), raw)
            expected = PrefixWitness(
                saved.root_id,
                file_revision(info),
                record.message.seq,
                digest.hex(),
                _tail(stream, info.st_size),
            )
            marker.seal_with(PendingSeal.capture(saved, expected, path))
            bus.write_metadata_unlocked(marker)
            with db:
                _index_row(db, offset, raw, record)
                PrefixCertificate.capture(
                    saved.root_id, info, record.message.seq, digest, expected.tail
                ).upsert(db)
            _directory_sync(path)
            marker.seal_with(FinalSeal.capture(expected, path))
            bus.write_metadata_unlocked(marker)
            return _saved(db)
    except (sqlite3.Error, OSError) as error:
        raise RelationViolationError("Private bus checkpoint append outcome UNKNOWN.") from error


def certified_delivery_page_unlocked(
    bus: WireLog,
    marker: WireMetadata,
    lookup: str,
    *,
    after: int = 0,
    limit: int = 100,
) -> tuple[PrefixWitness, tuple[CommittedDelivery, ...], bool]:
    """Complete addressed page; caller holds bus lock and must bind SQL/native proof.

    The returned through sequence is a global bus bound. The page witness's
    latest_source_seq is derived from this same sealed index connection under
    the final revision fence; only that field bounds initial-source coverage.
    Neither bound grants native-input acceptance, ACK, or skip permission.
    Recheck the witness under the same bus-held SQL commit; an unlocked return
    is advisory only.
    """
    from .bus_publication import CommittedDelivery, unique_wire_object

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
            rows = DeliverySources.read(
                db.execute(
                    f"SELECT i.seq,i.message_id,i.offset,i.length FROM {Addressed.declared_name} a "
                    f"JOIN {DeliverySources.declared_name} i ON i.seq=a.seq "
                    "WHERE a.lookup=? AND i.seq>? ORDER BY i.seq LIMIT ?",
                    (lookup, after, limit + 1),
                )
            )
            has_more = len(rows) > limit
            last = DeliverySources.read(
                db.execute(
                    f"SELECT * FROM {DeliverySources.declared_name} ORDER BY seq DESC LIMIT 1"
                )
            )
            latest_source_seq = last[0].seq if last else 0
            if latest_source_seq < 0 or latest_source_seq > witness.through_seq:
                raise RelationViolationError("Certified initial high-water is invalid.")
            initials = []
            for row in rows[:limit]:
                stream.seek(row.offset)
                raw = stream.read(row.length)
                if len(raw) != row.length or not raw.endswith(b"\n"):
                    raise RelationViolationError("Certified initial row changed.")
                record = json.loads(raw, object_pairs_hook=unique_wire_object)
                initial = CommittedDelivery.from_wire(record, witness.root_id)
                if (
                    initial.message.seq != row.seq
                    or initial.message.message_id != row.message_id
                    or not any(r.recipient_lookup == lookup for r in initial.audience.recipients)
                ):
                    raise RelationViolationError("Certified initial lookup differs from bus row.")
                initials.append(initial)
            if (
                file_revision(_path(bus.path).stat()) != marker.seal.db_revision
                or bus._private_marker_unlocked().seal != marker.seal
                or file_revision(bus.path.stat()) != witness.revision
            ):
                raise RelationViolationError("Certified page changed during its read fence.")
            return (
                replace(witness, latest_source_seq=latest_source_seq),
                tuple(initials),
                has_more,
            )
    except RelationViolationError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise RelationViolationError("Certified initial page is unavailable.") from error


def addressed_source_pointers_unlocked(
    bus: WireLog, marker: WireMetadata, lookup: str, *, limit: int = 4
) -> tuple[DeliverySources, ...]:
    """Latest source pointers for natural-turn awareness, never delivery evidence.

    Caller holds the bus lock. Read only the current sealed index: no payload
    decode, historical scan, index repair, recovery, or native cursor advancement.
    Frozen audience membership includes unmentioned NoWake observers.
    """
    path = _path(bus.path)
    with closing(_connect(path, readonly=True)) as db:
        saved = _saved(db)
        marker.seal.check_final(saved, path)
        if (
            saved.root_id != marker.root_id
            or saved.through_seq != marker.last_seq
            or file_revision(bus.path.stat()) != saved.revision
        ):
            raise RelationViolationError("Current source pointers require an unchanged checkpoint.")
        rows = DeliverySources.read(
            db.execute(
                f"SELECT i.seq,i.message_id,i.offset,i.length FROM {Addressed.declared_name} a "
                f"JOIN {DeliverySources.declared_name} i ON i.seq=a.seq "
                "WHERE a.lookup=? AND i.seq>? ORDER BY i.seq DESC LIMIT ?",
                (lookup, marker.admission_after_seq, limit),
            )
        )
        marker.seal.check_final(saved, path)
        if file_revision(bus.path.stat()) != saved.revision:
            raise RelationViolationError("Source changed during awareness read.")
        return tuple(reversed(rows))
