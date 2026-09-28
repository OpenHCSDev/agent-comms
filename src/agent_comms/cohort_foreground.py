"""One-shot foreground recipient owner for a private N/K root.

Start this process *before* publishing an initial cohort. It registers a fresh
recipient under its own PID, prints a ready receipt, accepts at most one
selected claim, then exits. The sender must separately initialize the private
protocol and publish the cohort after readiness. No inbox ACK, daemon, retry,
monitor, production migration, or recovery decision is made here.

    python -m agent_comms.cohort_foreground --root /var/tmp/my-private-wire \\
        --wire-root-id ID --name recipient --worktree /path/to/project \\
        --tags team --native-package /path/to/reviewed/copied/pi
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import stat
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .bus_publication import stable_thread_lookup
from .child_process import ProcessIdentity
from .cohort_schema import CohortDeliveryReceipts, install_private_cohort_schema
from .comms import Comms
from .coordinated_runtime import (
    CoordinatedTurn,
    SelectedExecution,
    SelectedExistingFileWrite,
)
from .coordinated_runtime_schema import install_native_runtime_schema
from .coordination_cohort import accept_initial_cohort, sealed_cohort_sequences
from .coordination_response import install_private_response_schema
from .coordination_store import IdentityConflict, MutationStore, PublicationActivationBlocked
from .envelope_claim_transitions import ExistingFileClaim
from .errors import RelationViolationError
from .message_bus import MessageBus
from .native_pi import _private_session_dir, _trusted_package
from .native_prompt_binding import install_prompt_binding_schema
from .private_registry_guard import _require_no_private_owner_rename
from .store_files import _store_lock
from .threads import Thread


@dataclass(frozen=True, slots=True)
class NoWakeReceipt:
    """A committed full-N observer receipt, not an absent selected claim."""

    wire_seq: int


def _preflight(root: Path, wire_root_id: str, native_package: Path, opt_in: bool) -> None:
    # Do not create a root, registry, SQLite database, or provider opportunity
    # when the owner-only directory or reviewed copied Pi is absent.
    if not opt_in:
        raise PublicationActivationBlocked("foreground cohort requires explicit activation")
    _private_session_dir(root)
    _trusted_package(native_package)
    comms = Comms(root)
    bus = MessageBus(root / "bus.jsonl", comms.registry, private_response_writes=True)
    with bus.log.locked():
        marker = bus.log._private_marker_unlocked()
    if marker.root_id != wire_root_id:
        raise IdentityConflict("private initial wire root changed")


def _accept_visible_initials(
    bus: MessageBus,
    root_id: str,
    store: MutationStore,
    lookup: str,
    after_seq: int,
    *,
    owner_name: str,
    native_package: Path | None = None,
) -> int:
    """Accept only committed initial rows addressed to this durable recipient.

    Take a bounded snapshot under the bus authority, then release the bus lock
    before the SQL transaction. All N identities must already be registered.
    Never infer a cohort from an ordinary public message or its body.
    """
    with bus.log.locked():
        _require_no_private_owner_rename(bus.log.path.parent)
        marker = bus.log._private_marker_unlocked()
        if marker.root_id != root_id:
            raise IdentityConflict("private initial wire root changed")
        after_seq = max(after_seq, marker.admission_after_seq)
        initials = tuple(
            initial
            for _message, _receipt, initial in bus.log._verified_private_rows_unlocked(marker)
            if initial is not None
            and initial.message.seq > after_seq
            and any(
                r.recipient_lookup == lookup and r.canonical_thread == owner_name
                for r in initial.audience.recipients
            )
        )
    sealed = sealed_cohort_sequences(store, root_id)
    unaccepted = tuple(initial for initial in initials if initial.message.seq not in sealed)
    if len(unaccepted) > 100:
        raise IdentityConflict("recipient initial cohort batch exceeds bounded foreground scan")
    if unaccepted and native_package is not None:
        # An ACP observation with new originals must still validate the package
        # before SQL acceptance. Sealed receipts require no repeated preflight.
        _preflight(bus.log.path.parent, root_id, native_package, True)
    for initial in unaccepted:
        # A prior canonical name is historical after a private owner rename.
        # Never create a NEW generation's selected attempt from that old
        # frozen recipient, or infer it was consumed.
        accept_initial_cohort(bus, root_id, initial.message.seq, store)
    return initials[-1].message.seq if initials else after_seq


async def run_foreground_once(
    root: Path,
    *,
    wire_root_id: str,
    name: str,
    worktree: Path,
    tags: frozenset[str],
    native_package: Path,
    opt_in: bool = True,
    wait_seconds: float = 60.0,
    ready: Callable[[Thread], None] | None = None,
    selected_existing_file_write: SelectedExistingFileWrite | None = None,
) -> CoordinatedTurn | NoWakeReceipt | None:
    """Register THIS PID as a new recipient; wait boundedly for one claim.

    A preexisting name is rejected, even if stopped: takeover/migration needs a
    separate verified all-old-writers-stop protocol. A failed or uncertain
    model attempt propagates immediately and is never invoked a second time.
    """
    root = Path(root).absolute()
    worktree = Path(worktree).absolute()
    if (
        type(wait_seconds) not in (float, int)
        or not math.isfinite(wait_seconds)
        or not 0 <= wait_seconds <= 300
        or not worktree.is_dir()
    ):
        raise ValueError("wait must be in [0,300] and worktree must exist")
    _preflight(root, wire_root_id, native_package, opt_in)
    if (
        selected_existing_file_write is not None
        and type(selected_existing_file_write) is not SelectedExistingFileWrite
    ):
        raise TypeError("foreground selected write needs a trusted explicit plan")
    comms = Comms(root)
    if selected_existing_file_write is not None:
        # Refuse an uninitialized claim protocol or permanently invalid
        # resource before owner registration or an irreversible native send.
        with comms.bus.log.locked():
            marker = comms.bus.log._private_marker_unlocked()
        if not marker.claims:
            raise PublicationActivationBlocked("selected file write needs a private claim protocol")
        selected_existing_file_write.resource.normalized(worktree)
    thread = Thread(
        name, tags, str(worktree), process_identity=ProcessIdentity.capture(os.getpid())
    )
    # The registry name reservation and registration must be ONE wire-locked
    # operation; `claim_thread` silently chooses a suffix on a collision.
    with _store_lock(comms._wire_lock_path):
        if comms.registry.name_reserved(name):
            raise IdentityConflict("recipient name already reserved; no takeover")
        comms.channels._require_available_new_tags(tags)
        comms.registry.register(thread)
    try:
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            install_prompt_binding_schema(store)
            lookup = stable_thread_lookup(comms.registry.require(name).created_at)
            store.register_participant(lookup, name, name, committed=True)
        if ready is not None:
            ready(thread)
        deadline = time.monotonic() + wait_seconds
        bus = MessageBus(root / "bus.jsonl", comms.registry, private_response_writes=True)
        cursor = 0
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            while True:
                cursor = _accept_visible_initials(
                    bus, wire_root_id, store, lookup, cursor, owner_name=thread.name
                )
                # Even an empty scan checks this PID against the live registry.
                # A terminal claim cannot be replayed by this foreground owner.
                result = await SelectedExecution(
                    root=root,
                    wire_root_id=wire_root_id,
                    owner_name=name,
                    native_package=native_package,
                    opt_in=True,
                    selected_existing_file_write=selected_existing_file_write,
                ).run()
                if result is not None:
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with store._read_transaction():
                        observers = CohortDeliveryReceipts.read(
                            store._connection.execute(
                                "SELECT d.* FROM cohort_delivery_receipts d "
                                "JOIN claim_batch_receipts r ON r.wire_root_id=d.wire_root_id "
                                "AND r.wire_seq=d.wire_seq WHERE r.sealed=1 "
                                "AND d.wire_root_id=? AND d.recipient_lookup=? "
                                "AND d.kind='unmentioned_observer' AND d.wire_seq<=? "
                                "ORDER BY d.wire_seq DESC LIMIT 1",
                                (wire_root_id, lookup, cursor),
                            )
                        )
                    return NoWakeReceipt(observers[0].wire_seq) if observers else None
                await asyncio.sleep(min(0.1, remaining))
    finally:
        # Do not stop a replacement owner. A killed process may leave a stale
        # RUNNING PID: that state requires explicit manual disposition, not an
        # automatic takeover of an uncertain claim.
        with _store_lock(comms._wire_lock_path):
            try:
                current, _generation = comms.registry.live_owner_with_admission(name)
            except (RelationViolationError, ValueError):
                pass
            else:
                if current.pid == os.getpid() and current.created_at == thread.created_at:
                    comms.registry.unregister(name)


def _read_selected_write_source(path: Path) -> bytes:
    """Read explicit operator input before reserving an owner or native input."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("selected source requires no-follow descriptors")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 1024 * 1024:
        raise ValueError("selected source must be a bounded regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 1024 * 1024
            or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError("selected source must be a bounded regular file")
        contents = os.read(fd, 1024 * 1024 + 1)
        if len(contents) > 1024 * 1024:
            raise ValueError("selected source exceeds 1 MiB")
        return contents
    finally:
        os.close(fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--wire-root-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--tags", default="", help="Comma-separated recipient tags")
    parser.add_argument("--native-package", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument(
        "--selected-write-resource",
        type=Path,
        help="Existing file under the selected owner's worktree; requires --selected-write-source",
    )
    parser.add_argument(
        "--selected-write-source",
        type=Path,
        help="Bounded regular source of bytes for a successful selected FULL file replacement",
    )
    parser.add_argument("--opt-in", action="store_true", default=True, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    def ready(thread: Thread) -> None:
        print(json.dumps({"ready": True, "name": thread.name, "pid": os.getpid()}), flush=True)

    try:
        if (args.selected_write_resource is None) != (args.selected_write_source is None):
            raise ValueError("selected write requires both resource and source")
        if args.selected_write_resource is not None:
            _preflight(
                Path(args.root).absolute(), args.wire_root_id, args.native_package, args.opt_in
            )
        selected_write = (
            SelectedExistingFileWrite(
                ExistingFileClaim(args.selected_write_resource),
                _read_selected_write_source(args.selected_write_source),
            )
            if args.selected_write_resource is not None
            else None
        )
        result = asyncio.run(
            run_foreground_once(
                args.root,
                wire_root_id=args.wire_root_id,
                name=args.name,
                worktree=args.worktree,
                tags=frozenset(filter(None, args.tags.split(","))),
                native_package=args.native_package,
                opt_in=args.opt_in,
                wait_seconds=args.wait_seconds,
                ready=ready,
                selected_existing_file_write=selected_write,
            )
        )
    except Exception as error:
        # Model/provider diagnostics or message bodies must never become a
        # public command-line receipt. Retain the owner-only root for diagnosis.
        print(json.dumps({"error_type": type(error).__name__, "root_retained": True}), flush=True)
        return 1
    print(
        json.dumps(
            {"disposition": "NO_WAKE", "wire_seq": result.wire_seq}
            if isinstance(result, NoWakeReceipt)
            else (
                {
                    "disposition": result.disposition.declared_name,
                    "claim_id": result.assignment_id,
                    "response_message_id": result.response_message_id,
                }
                if result is not None
                else {"disposition": "NO_SELECTED_CLAIM"}
            )
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
