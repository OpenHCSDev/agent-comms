"""Opt-in, append-writer-maintained private bus prefix certificate.

The JSONL bus remains authoritative. Only the canonical private MessageBus writer may
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
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast

if TYPE_CHECKING:
    from .bus_publication import CommittedInitial
    from .declarations import Message, MessageBus

_VERSION = 1
_SEED = hashlib.sha256(b"agent-comms:private-bus-prefix:v1\0").digest()
_TAIL_BYTES = 4096


@dataclass(frozen=True, slots=True)
class PrefixWitness:
    root_id: str
    device: int
    inode: int
    offset: int
    through_seq: int
    digest: str
    tail: str
    revision: tuple[int, int, int, int, int]
    # Derived only by certified_initial_page_unlocked from the sealed index;
    # absent on a bare bus witness and not part of the persistent wire seal.
    latest_initial_seq: int = field(default=0, compare=False)


def _witness_record(witness: PrefixWitness) -> dict[str, object]:
    return {
        "root_id": witness.root_id,
        "revision": list(witness.revision),
        "through_seq": witness.through_seq,
        "digest": witness.digest,
        "tail": witness.tail,
    }


def _valid_witness_record(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == {"root_id", "revision", "through_seq", "digest", "tail"}
        and type(value["root_id"]) is str
        and len(value["root_id"]) == 32
        and type(value["revision"]) is list
        and len(value["revision"]) == 5
        and all(type(item) is int and item >= 0 for item in value["revision"])
        and type(value["through_seq"]) is int
        and value["through_seq"] >= 0
        and all(
            type(value[name]) is str
            and len(value[name]) == 64
            and all(character in "0123456789abcdef" for character in value[name])
            for name in ("digest", "tail")
        )
    )


def _final_seal(witness: PrefixWitness, db_path: Path) -> dict[str, object]:
    return {
        "version": _VERSION,
        "state": "final",
        "db_revision": list(_revision(db_path.stat())),
        "witness": _witness_record(witness),
    }


def _pending_seal(
    prior: PrefixWitness, expected: PrefixWitness, db_path: Path
) -> dict[str, object]:
    return {
        "version": _VERSION,
        "state": "pending",
        "db_revision": list(_revision(db_path.stat())),
        "prior": _witness_record(prior),
        "expected": _witness_record(expected),
    }


def _write_seal(bus: MessageBus, marker: Mapping[str, int | str], seal: dict[str, object]) -> None:
    from .declarations import _atomic_write_text

    # The canonical marker reader returns a validated mutable dict; its
    # historical value annotation predates the optional nested seal.
    mutable_marker = cast(dict[str, object], marker)
    mutable_marker["checkpoint_seal"] = seal
    _atomic_write_text(
        bus._path.parent / "bus_meta.json", json.dumps(mutable_marker, indent=2), fsync_parent=True
    )


def _check_final_seal(marker: Mapping[str, object], saved: PrefixWitness, db_path: Path) -> None:
    seal = marker.get("checkpoint_seal")
    if (
        marker.get("checkpoint_version") != _VERSION
        or type(seal) is not dict
        or set(seal) != {"version", "state", "db_revision", "witness"}
        or seal["version"] != _VERSION
        or seal["state"] != "final"
        or seal["witness"] != _witness_record(saved)
        or seal["db_revision"] != list(_revision(db_path.stat()))
    ):
        raise _failure("Private bus checkpoint index seal changed or is pending.")


def _failure(message: str) -> Exception:
    from .declarations import RelationViolationError

    return RelationViolationError(message)


def _path(bus_path: Path) -> Path:
    return bus_path.with_name("private_bus_checkpoint.sqlite3")


def _revision(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _tail(stream: BinaryIO, offset: int) -> str:
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
    from .declarations import RelationViolationError

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
    if row is None or len(row) != 11 or row["version"] != _VERSION:
        raise _failure("Private bus checkpoint schema is unavailable.")
    fields = (
        "root_id",
        "device",
        "inode",
        "offset",
        "through_seq",
        "digest",
        "tail",
        "mtime_ns",
        "ctime_ns",
    )
    if any(row[key] is None for key in fields):
        raise _failure("Private bus checkpoint is incomplete.")
    digest, tail = row["digest"], row["tail"]
    if (
        type(row["root_id"]) is not str
        or len(row["root_id"]) != 32
        or any(c not in "0123456789abcdef" for c in row["root_id"])
        or any(
            type(row[k]) is not int or row[k] < 0
            for k in ("device", "inode", "offset", "through_seq", "mtime_ns", "ctime_ns")
        )
        or type(digest) is not str
        or len(digest) != 64
        or type(tail) is not str
        or len(tail) != 64
        or any(c not in "0123456789abcdef" for c in digest + tail)
    ):
        raise _failure("Private bus checkpoint identity is malformed.")
    return PrefixWitness(
        row["root_id"],
        row["device"],
        row["inode"],
        row["offset"],
        row["through_seq"],
        digest,
        tail,
        (row["device"], row["inode"], row["offset"], row["mtime_ns"], row["ctime_ns"]),
    )


def _set_certificate(
    db: sqlite3.Connection, root_id: str, info: os.stat_result, seq: int, digest: bytes, tail: str
) -> None:
    db.execute(
        "UPDATE certificate SET root_id=?,device=?,inode=?,offset=?,through_seq=?,"
        "digest=?,tail=?,mtime_ns=?,ctime_ns=? WHERE singleton=1",
        (
            root_id,
            info.st_dev,
            info.st_ino,
            info.st_size,
            seq,
            digest.hex(),
            tail,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ),
    )


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


def install_private_bus_checkpoint(bus: MessageBus) -> PrefixWitness:
    """Explicit fresh-root opt-in; never auto-install or migrate an old wire."""
    from .declarations import MessageBus, _atomic_write_text, _store_lock

    if type(bus) is not MessageBus or bus._path.name != "bus.jsonl":
        raise TypeError("Canonical private MessageBus required")
    with _store_lock(bus._path):
        marker = bus._private_marker_unlocked()
        if marker.get("claim_envelopes_version") != 1 or marker["last_seq"] != 0:
            raise _failure("Checkpoint installation needs an empty claim-enabled private root.")
        path = _path(bus._path)
        if path.exists() or path.is_symlink() or (bus._path.exists() and bus._path.stat().st_size):
            raise _failure("Checkpoint installation requires a fresh empty private bus.")
        fd = os.open(
            bus._path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _directory_sync(bus._path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        try:
            with closing(_connect(path)) as db:
                db.executescript(
                    "CREATE TABLE certificate(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                    "version INTEGER NOT NULL,root_id TEXT NOT NULL,device INTEGER NOT NULL,"
                    "inode INTEGER NOT NULL,offset INTEGER NOT NULL,through_seq INTEGER NOT NULL,"
                    "digest TEXT NOT NULL,tail TEXT NOT NULL,mtime_ns INTEGER NOT NULL,"
                    "ctime_ns INTEGER NOT NULL);"
                    "CREATE TABLE response_keys(key TEXT PRIMARY KEY);"
                    "CREATE TABLE initials(seq INTEGER PRIMARY KEY,message_id TEXT NOT NULL,"
                    "offset INTEGER NOT NULL,length INTEGER NOT NULL);"
                    "CREATE TABLE addressed(lookup TEXT NOT NULL,seq INTEGER NOT NULL,"
                    "PRIMARY KEY(lookup,seq));"
                )
                with bus._path.open("rb") as stream:
                    info = os.fstat(stream.fileno())
                    tail = _tail(stream, 0)
                with db:
                    db.execute(
                        "INSERT INTO certificate VALUES(1,?,?,?,?,?,?,?,?,?,?)",
                        (
                            _VERSION,
                            marker["wire_root_id"],
                            info.st_dev,
                            info.st_ino,
                            0,
                            0,
                            _SEED.hex(),
                            tail,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                        ),
                    )
            _directory_sync(path)
            with closing(_connect(path, readonly=True)) as db:
                witness = _saved(db)
            marker["checkpoint_version"] = _VERSION
            cast(dict[str, object], marker)["checkpoint_seal"] = _final_seal(witness, path)
            _atomic_write_text(
                bus._path.parent / "bus_meta.json",
                json.dumps(marker, indent=2),
                fsync_parent=True,
            )
            return witness
        except BaseException:
            # A failed install must not turn a partly initialized sidecar into authority.
            # Preserve the private bus and quarantine the checkpoint for inspection.
            raise


def certificate_enabled(bus_path: Path) -> bool:
    path = _path(bus_path)
    return path.exists() or path.is_symlink()


def _recover_pending_unlocked(
    bus: MessageBus,
    marker: Mapping[str, int | str],
    db: sqlite3.Connection,
    db_path: Path,
    saved: PrefixWitness,
    info: os.stat_result,
) -> PrefixWitness:
    """Repair only a durable writer intent after a COMPLETE canonical bus scan."""
    seal = marker.get("checkpoint_seal")
    if (
        type(seal) is not dict
        or set(seal) != {"version", "state", "db_revision", "prior", "expected"}
        or seal["version"] != _VERSION
        or seal["state"] != "pending"
        or not _valid_witness_record(seal["prior"])
        or not _valid_witness_record(seal["expected"])
        or _witness_record(saved) not in (seal["prior"], seal["expected"])
        or seal["expected"].get("revision") != list(_revision(info))
        or seal["prior"].get("root_id") != marker["wire_root_id"]
    ):
        raise _failure("Private bus checkpoint pending intent is inconsistent.")
    prior = seal["prior"]
    digest = _SEED
    prior_seen = prior["revision"][2] == 0
    prior_seq = 0
    last_seq = 0
    count = 0
    size = 0

    with db:
        db.execute("DELETE FROM addressed")
        db.execute("DELETE FROM initials")
        db.execute("DELETE FROM response_keys")

        def collect(
            offset: int,
            raw: bytes,
            message: Message,
            receipt: Mapping[str, object] | None,
            initial: CommittedInitial | None,
        ) -> None:
            nonlocal digest, prior_seen, prior_seq, last_seq, count, size
            count += 1
            size += len(raw)
            if count > 100_000 or size > 128 * 1024 * 1024:
                raise _failure("Private bus pending recovery exceeds cold bound.")
            digest = _chain(digest, raw)
            if offset + len(raw) == prior["revision"][2]:
                prior_seen = True
                prior_seq = message.seq
                if digest.hex() != prior["digest"] or prior_seq != prior["through_seq"]:
                    raise _failure("Private bus checkpoint old prefix changed during recovery.")
            last_seq = message.seq
            _index_row(db, offset, raw, message, receipt, initial)

        for _ in bus._verified_private_rows_unlocked(marker, on_row=collect):
            pass
        if not prior_seen or (prior["revision"][2] == 0 and prior["digest"] != _SEED.hex()):
            raise _failure("Private bus checkpoint old prefix is unavailable.")
        if _revision(bus._path.stat()) != _revision(info):
            raise _failure("Private bus changed during pending recovery.")
        with bus._path.open("rb") as stream:
            expected = PrefixWitness(
                str(marker["wire_root_id"]),
                info.st_dev,
                info.st_ino,
                info.st_size,
                last_seq,
                digest.hex(),
                _tail(stream, info.st_size),
                _revision(info),
            )
        if _witness_record(expected) != seal["expected"] or last_seq > _marker_last_seq(marker):
            raise _failure("Private bus checkpoint pending suffix differs from intent.")
        _set_certificate(db, expected.root_id, info, last_seq, digest, expected.tail)
    _directory_sync(db_path)
    _write_seal(bus, marker, _final_seal(expected, db_path))
    return expected


def _marker_last_seq(marker: Mapping[str, int | str]) -> int:
    value = marker.get("last_seq")
    if type(value) is not int:
        raise _failure("Private bus sequence marker is invalid.")
    return value


def verify_private_bus_checkpoint_unlocked(
    bus: MessageBus, marker: Mapping[str, int | str]
) -> PrefixWitness:
    """Check an exact revision or cold-validate every old byte on a changed revision.

    Caller holds the bus lock and has fsynced the bus inode and directory.
    """
    path = _path(bus._path)
    try:
        with closing(_connect(path)) as db, bus._path.open("rb") as stream:
            saved = _saved(db)
            info = os.fstat(stream.fileno())
            if (
                marker.get("checkpoint_version") != _VERSION
                or saved.root_id != marker["wire_root_id"]
                or (saved.device, saved.inode) != (info.st_dev, info.st_ino)
                or info.st_size < saved.offset
            ):
                raise _failure("Private bus checkpoint root/inode/size changed.")
            seal = marker.get("checkpoint_seal")
            if type(seal) is dict and seal.get("state") == "pending":
                return _recover_pending_unlocked(bus, marker, db, path, saved, info)
            _check_final_seal(marker, saved, path)
            if _tail(stream, saved.offset) != saved.tail:
                raise _failure("Private bus checkpoint prefix tail changed.")
            if saved.through_seq > _marker_last_seq(marker):
                raise _failure("Private bus checkpoint exceeds the durable sequence marker.")
            if _revision(info) == saved.revision:
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
            if _revision(os.fstat(stream.fileno())) != _revision(info) or _revision(
                bus._path.stat()
            ) != _revision(info):
                raise _failure("Private bus changed during checkpoint validation.")
            # Full parser above checked all cross-prefix response key duplicates.
            last_seq = additions[-1][2].seq if additions else saved.through_seq
            expected = PrefixWitness(
                saved.root_id,
                info.st_dev,
                info.st_ino,
                info.st_size,
                last_seq,
                digest.hex(),
                _tail(stream, info.st_size),
                _revision(info),
            )
            _write_seal(bus, marker, _pending_seal(saved, expected, path))
            with db:
                for offset, raw, message, receipt, initial in additions:
                    _index_row(db, offset, raw, message, receipt, initial)
                _set_certificate(db, saved.root_id, info, last_seq, digest, expected.tail)
            _directory_sync(path)
            _write_seal(bus, marker, _final_seal(expected, path))
            return _saved(db)
    except (sqlite3.Error, OSError) as error:
        raise _failure("Private bus checkpoint verification is unavailable.") from error


def append_private_bus_checkpoint_unlocked(
    bus: MessageBus,
    marker: Mapping[str, int | str],
    raw: bytes,
    message: Message,
    receipt: Mapping[str, object] | None,
    initial: CommittedInitial | None,
) -> PrefixWitness:
    """Append one certified row only AFTER the canonical bus/parent fsync."""
    path = _path(bus._path)
    try:
        with closing(_connect(path)) as db, bus._path.open("rb") as stream:
            saved = _saved(db)
            info = os.fstat(stream.fileno())
            offset = info.st_size - len(raw)
            if (
                marker.get("checkpoint_version") != _VERSION
                or saved.root_id != marker["wire_root_id"]
                or (saved.device, saved.inode) != (info.st_dev, info.st_ino)
                or offset != saved.offset
                or message.seq <= saved.through_seq
            ):
                raise _failure("Private bus checkpoint append lost its prefix fence.")
            _check_final_seal(marker, saved, path)
            stream.seek(offset)
            if stream.read(len(raw)) != raw or _tail(stream, offset) != saved.tail:
                raise _failure("Private bus checkpoint append bytes differ.")
            digest = _chain(bytes.fromhex(saved.digest), raw)
            expected = PrefixWitness(
                saved.root_id,
                info.st_dev,
                info.st_ino,
                info.st_size,
                message.seq,
                digest.hex(),
                _tail(stream, info.st_size),
                _revision(info),
            )
            _write_seal(bus, marker, _pending_seal(saved, expected, path))
            with db:
                _index_row(db, offset, raw, message, receipt, initial)
                _set_certificate(db, saved.root_id, info, message.seq, digest, expected.tail)
            _directory_sync(path)
            _write_seal(bus, marker, _final_seal(expected, path))
            return _saved(db)
    except (sqlite3.Error, OSError) as error:
        raise _failure("Private bus checkpoint append outcome UNKNOWN.") from error


def certified_initial_page_unlocked(
    bus: MessageBus,
    marker: Mapping[str, int | str],
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
    from .declarations import RelationViolationError

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
            closing(_connect(_path(bus._path), readonly=True)) as db,
            bus._path.open("rb") as stream,
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
                list(_revision(_path(bus._path).stat()))
                != cast(dict[str, object], marker["checkpoint_seal"])["db_revision"]
                or bus._private_marker_unlocked().get("checkpoint_seal")
                != marker["checkpoint_seal"]
                or _revision(bus._path.stat()) != witness.revision
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
