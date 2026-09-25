"""One-shot foreground recipient owner for a private N/K root.

Start this process *before* publishing an initial cohort. It registers a fresh
recipient under its own PID, prints a ready receipt, accepts at most one
selected claim, then exits. The sender must separately initialize the private
protocol and publish the cohort after readiness. No inbox ACK, daemon, retry,
monitor, production cutover, or recovery decision is made here.

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
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .bus_publication import stable_thread_lookup
from .cohort_schema import install_private_cohort_schema
from .coordinated_runtime import CoordinatedTurn, run_one_sealed_claim
from .coordinated_runtime_schema import install_native_runtime_schema
from .coordination_cohort import accept_initial_cohort
from .coordination_response import install_private_response_schema
from .coordination_store import IdentityConflict, MutationStore, PublicationActivationBlocked
from .declarations import MessageBus, RelationViolationError, Thread, _store_lock
from .native_pi import _private_session_dir, _trusted_package
from .operations import Comms


@dataclass(frozen=True, slots=True)
class NoWakeReceipt:
    """A committed full-N observer receipt, not an absent selected claim."""

    wire_seq: int


def _preflight(root: Path, wire_root_id: str, native_package: Path, opt_in: bool) -> None:
    # Do not create a root, registry, SQLite database, or provider opportunity
    # when the owner-only directory or reviewed copied Pi is absent.
    if not opt_in or root == Path("/var/tmp") or not root.is_relative_to("/var/tmp"):
        raise PublicationActivationBlocked("foreground cohort requires a private /var/tmp root")
    _private_session_dir(root)
    _trusted_package(native_package)
    comms = Comms(root)
    bus = MessageBus(root / "bus.jsonl", comms.registry, private_response_writes=True)
    with _store_lock(bus._path):
        marker = bus._private_marker_unlocked()
    if marker["wire_root_id"] != wire_root_id:
        raise IdentityConflict("private initial wire root changed")


def _accept_visible_initials(
    bus: MessageBus, root_id: str, store: MutationStore, lookup: str, after_seq: int
) -> int:
    """Accept only committed initial rows addressed to this durable recipient.

    Take a bounded snapshot under the bus authority, then release the bus lock
    before the SQL transaction. All N identities must already be registered.
    Never infer a cohort from an ordinary public message or its body.
    """
    with _store_lock(bus._path):
        marker = bus._private_marker_unlocked()
        if marker["wire_root_id"] != root_id:
            raise IdentityConflict("private initial wire root changed")
        initials = tuple(
            initial
            for _message, _receipt, initial in bus._verified_private_rows_unlocked(marker)
            if initial is not None and initial.message.seq > after_seq
        )
    if len(initials) > 100:
        raise IdentityConflict("initial cohort batch exceeds bounded foreground scan")
    for initial in initials:
        if any(r.recipient_lookup == lookup for r in initial.audience.recipients):
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
) -> CoordinatedTurn | NoWakeReceipt | None:
    """Register THIS PID as a new recipient; wait boundedly for one claim.

    A preexisting name is rejected, even if stopped: takeover/cutover needs a
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
    comms = Comms(root)
    thread = Thread(name, tags, str(worktree), pid=os.getpid())
    # The registry name reservation and registration must be ONE wire-locked
    # operation; `claim_thread` silently chooses a suffix on a collision.
    with _store_lock(comms._wire_lock_path):
        if comms.registry.name_reserved(name):
            raise IdentityConflict("recipient name already reserved; no takeover")
        comms._require_available_new_tags(tags)
        comms.registry.register(thread)
    try:
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            install_private_cohort_schema(store)
            install_private_response_schema(store)
            install_native_runtime_schema(store)
            lookup = stable_thread_lookup(comms.registry.require(name).created_at)
            store.register_participant(lookup, name, name, committed=True)
        if ready is not None:
            ready(thread)
        deadline = time.monotonic() + wait_seconds
        bus = MessageBus(root / "bus.jsonl", comms.registry, private_response_writes=True)
        cursor = 0
        with MutationStore(str(root / "coordination.sqlite3")) as store:
            while True:
                cursor = _accept_visible_initials(bus, wire_root_id, store, lookup, cursor)
                # Even an empty scan checks this PID against the live registry.
                # A terminal claim cannot be replayed by this foreground owner.
                result = await run_one_sealed_claim(
                    root,
                    wire_root_id=wire_root_id,
                    owner_name=name,
                    native_package=native_package,
                    opt_in=True,
                )
                if result is not None:
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with store._read_transaction():
                        observer = store._connection.execute(
                            "SELECT d.wire_seq FROM cohort_delivery_receipts d "
                            "JOIN claim_batch_receipts r ON r.wire_root_id=d.wire_root_id "
                            "AND r.wire_seq=d.wire_seq WHERE r.sealed=1 "
                            "AND d.wire_root_id=? AND d.recipient_lookup=? "
                            "AND d.kind='unmentioned_observer' AND d.wire_seq<=? "
                            "ORDER BY d.wire_seq DESC LIMIT 1",
                            (wire_root_id, lookup, cursor),
                        ).fetchone()
                    return NoWakeReceipt(observer[0]) if observer else None
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--wire-root-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--tags", default="", help="Comma-separated recipient tags")
    parser.add_argument("--native-package", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, default=60.0)
    parser.add_argument("--opt-in", action="store_true", default=True, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    def ready(thread: Thread) -> None:
        print(json.dumps({"ready": True, "name": thread.name, "pid": os.getpid()}), flush=True)

    try:
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
                    "disposition": result.disposition.value,
                    "claim_id": result.claim_id,
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
