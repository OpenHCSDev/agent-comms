"""Explicit one-shot foreground owner for an already initialized private N/K root.

The controlling process must create the owner-only /var/tmp root, initialize the
private cohort/schema, start this process and wait for READY, publish/accept the
initial cohort, then send exactly one ``GO <last-seen-seq>`` line on stdin. This
is not a daemon, monitor, production cutover, legacy inbox ACK or retry loop.
An uncertain attempt and its private root remain for manual disposition.
The explicit CLI switches are NOT an output/spend cap. Never invoke a paid
provider until a separate reviewed enforced cap exists; tests use a fake model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import select
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .coordinated_runtime import CoordinatedTurn, run_one_sealed_claim
from .coordination_store import PublicationActivationBlocked
from .declarations import RelationViolationError, Thread, _store_lock
from .native_pi import _private_session_dir, _trusted_package
from .operations import Comms

_MAX_GO_WAIT_SECONDS = 120
_GO = re.compile(r"GO (0|[1-9][0-9]{0,17})\Z")


@dataclass(slots=True)
class ForegroundOwner:
    """The registration belongs to this process and authorizes at most one GO."""

    root: Path
    wire_root_id: str
    name: str
    native_package: Path
    _started: bool = field(default=False, init=False, repr=False)

    async def run_go(
        self,
        line: str,
        *,
        executor: Callable[..., Awaitable[CoordinatedTurn | None]] = run_one_sealed_claim,
    ) -> CoordinatedTurn | None:
        match = _GO.fullmatch(line)
        if match is None:
            raise ValueError("Expected one GO <nonnegative sequence> command")
        if self._started:
            raise PublicationActivationBlocked("Foreground owner accepts exactly one GO")
        self._started = True  # Before the first await: even an uncertain attempt is spent.
        # The executor is injectable only by an in-process offline test. The CLI
        # always uses the pinned native executor; it never imports a fixture.
        return await executor(
            self.root,
            wire_root_id=self.wire_root_id,
            owner_name=self.name,
            native_package=self.native_package,
            opt_in=True,
            after_seq=int(match.group(1)),
        )


def reserve_foreground_owner(
    root: Path,
    *,
    wire_root_id: str,
    name: str,
    task: str,
    tags: frozenset[str],
    native_package: Path,
    opt_in: bool = True,
) -> ForegroundOwner:
    """Atomically reserve a fresh recipient in THIS process before publication.

    Never attach to/replace an existing owner; even a stopped owner may retain
    an uncertain model input. This only works on an initialized private root.
    """
    root = Path(root).absolute()
    if (
        type(opt_in) is not bool
        or not opt_in
        or root == Path("/var/tmp")
        or not root.is_relative_to("/var/tmp")
    ):
        raise PublicationActivationBlocked("Foreground N/K requires a private /var/tmp root")
    _private_session_dir(root)
    if type(wire_root_id) is not str or not wire_root_id:
        raise ValueError("A private wire-root ID is required")
    package = Path(native_package).absolute()
    _trusted_package(package)  # Reject before touching the recipient registry.
    worktree = root / "work"
    _private_session_dir(worktree)
    comms = Comms(root)
    with _store_lock(comms.bus._path):
        marker = comms.bus._private_marker_unlocked()
    if marker["wire_root_id"] != wire_root_id:
        raise RelationViolationError("Private wire root changed before owner reservation")
    owner = Thread(name, tags, str(worktree), pid=os.getpid(), task=task)
    # Ordinary Comms.register intentionally supports replacing idle owners. That
    # is NOT safe for a private foreground attempt: serialize the exact fresh
    # check and registry write against every public Comms.register/send caller
    # on the same wire lock without changing the shared operations module.
    with _store_lock(comms._wire_lock_path):
        canonical = comms.registry.canonical_name(owner.name)
        if canonical != owner.name or canonical in comms.registry.all_threads():
            raise RelationViolationError("Foreground recipient already exists")
        comms._require_available_new_tags(owner.tags)
        comms.registry.register(owner)
        comms.channel_catalog.remember_tags(owner.tags, owner.created_at)
    return ForegroundOwner(root, wire_root_id, owner.name, package)


def _emit(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _read_go_command(timeout: int) -> str:
    """Bound the COMPLETE stdin frame, not just the first readable byte."""
    deadline = time.monotonic() + timeout
    descriptor = sys.stdin.fileno()
    frame = bytearray()
    while len(frame) < 64:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise TimeoutError("Explicit GO did not complete before the foreground deadline")
        byte = os.read(descriptor, 1)
        if not byte:
            raise ValueError("Incomplete GO command")
        if byte == b"\n":
            try:
                return frame.decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise ValueError("GO command must be ASCII") from error
        frame.extend(byte)
    raise ValueError("GO command exceeds the private frame limit")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-comms-nk-foreground", description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--wire-root-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--tags", default="")
    parser.add_argument("--native-package", type=Path, required=True)
    parser.add_argument(
        "--private-opt-in", action="store_true", default=True, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--native-model-opt-in", action="store_true", default=True, help=argparse.SUPPRESS
    )
    parser.add_argument("--go-timeout", type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.go_timeout <= _MAX_GO_WAIT_SECONDS:
        return _error("GO timeout must be between 1 and 120 seconds")
    try:
        owner = reserve_foreground_owner(
            args.root,
            wire_root_id=args.wire_root_id,
            name=args.name,
            task=args.task,
            tags=frozenset(tag.strip() for tag in args.tags.split(",") if tag.strip()),
            native_package=args.native_package,
            opt_in=args.private_opt_in and args.native_model_opt_in,
        )
        _emit({"status": "ready", "name": owner.name, "pid": os.getpid()})
        line = _read_go_command(args.go_timeout)
        if line == "STOP":
            _emit(
                {
                    "status": "go_declined",
                    "name": owner.name,
                    "registration": "retained_for_manual_disposition",
                }
            )
            return 0
        result = asyncio.run(owner.run_go(line))
        _emit(
            {
                "status": "terminal",
                "name": owner.name,
                "disposition": result.disposition.value if result else None,
                "response_id": result.response_message_id if result else None,
                "route": result.exact_target if result else None,
            }
        )
        return 0
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        # Never include provider stderr, prompt text, or private root contents.
        return _error(f"Foreground N/K failed closed: {type(error).__name__}")


def _error(message: str) -> int:
    _emit({"error": message})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
