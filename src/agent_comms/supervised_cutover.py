"""Read-only, Linux-specific owner inventory for a supervised root migration.

This is a witness, not a stop permission. The operator must recapture it after
quiescence and preserve the old wire before any route is installed.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .declarations import RelationViolationError, Thread
from .input_disposition import InputDispositions
from .operations import Comms


@dataclass(frozen=True, slots=True)
class OwnerWitness:
    name: str
    pid: int
    created_at: float
    admission_generation: int
    process_start_ticks: int
    agent_bin: str
    agent_args: str


@dataclass(frozen=True, slots=True)
class LegacyInventory:
    root: Path
    live_owners: tuple[OwnerWitness, ...]
    dead_registry_owners: tuple[str, ...]
    active_turns: tuple[str, ...]
    pending_by_thread: tuple[tuple[str, int], ...]
    unknown_inputs: int


def _process_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except FileNotFoundError:
        return None
    end = raw.rfind(b") ")
    if end < 0 or raw[: raw.find(b" ")] != str(pid).encode():
        raise RelationViolationError("Owner process identity has malformed proc status")
    fields = raw[end + 2 :].split()
    if len(fields) <= 19:
        raise RelationViolationError("Owner process status omits its start time")
    if fields[0] == b"Z":
        return None
    return int(fields[19])


def _owner_witness(comms: Comms, thread: Thread, generation: int) -> OwnerWitness | None:
    start = _process_start_ticks(thread.pid)
    if start is None:
        return None
    if not comms._is_local_participant(thread, wait=False):
        raise RelationViolationError(f"Live owner {thread.name!r} is not bound to this wire")
    try:
        entries = Path(f"/proc/{thread.pid}/environ").read_bytes().split(b"\0")
    except OSError as error:
        raise RelationViolationError("Cannot inspect the live owner's launch settings") from error
    environment = dict(item.split(b"=", 1) for item in entries if b"=" in item)
    try:
        agent_bin = environment[b"AGENT_COMMS_AGENT_BIN"].decode("utf-8")
        agent_args = environment.get(b"AGENT_COMMS_AGENT_ARGS", b"").decode("utf-8")
    except (KeyError, UnicodeError) as error:
        raise RelationViolationError("Live owner has no reusable agent launch settings") from error
    if not agent_bin or start != _process_start_ticks(thread.pid):
        raise RelationViolationError("Owner process changed during inventory")
    return OwnerWitness(
        thread.name,
        thread.pid,
        thread.created_at,
        generation,
        start,
        agent_bin,
        agent_args,
    )


def inventory_legacy_root(comms: Comms) -> LegacyInventory:
    """Read existing authorities and fail on owner identity races.

    Pending/UNKNOWN counts are advisory while old writers run; the final
    stop barrier must take a fresh immutable archive before switching roots.
    """
    if not sys.platform.startswith("linux") or comms.root.resolve() != Path.home() / ".agent-comms":
        raise ValueError("Legacy owner inventory requires the local Linux comms root")
    before = comms.registry.snapshot()
    owners: list[OwnerWitness] = []
    dead: list[str] = []
    active_turns: list[str] = []
    pending: list[tuple[str, int]] = []
    for name, thread in sorted(before.threads.items()):
        if thread.active_turn is not None:
            active_turns.append(name)
        undelivered = len(comms.inbox(name))
        if undelivered:
            pending.append((name, undelivered))
        if not before.statuses[name].active or not thread.role.executable:
            continue
        if thread.pid <= 0:
            dead.append(name)
            continue
        generation = before.admission_generations.get(name)
        if generation is None:
            raise RelationViolationError("Live owner has no admission generation")
        witness = _owner_witness(comms, thread, generation)
        if witness is None:
            dead.append(name)
        else:
            owners.append(witness)
    after = comms.registry.snapshot()
    if (
        before.threads != after.threads
        or before.statuses != after.statuses
        or before.admission_generations != after.admission_generations
    ):
        raise RelationViolationError("Legacy registry changed during cutover inventory")
    rows = InputDispositions(comms.root)._read()
    return LegacyInventory(
        comms.root,
        tuple(owners),
        tuple(dead),
        tuple(active_turns),
        tuple(pending),
        sum(row["status"] == "unknown" for row in rows.values()),
    )
