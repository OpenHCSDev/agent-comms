"""One-shot quiet installation; remove after the accepted live cutover.

Default invocation only reports readiness. --apply closes admissions before
stopping idle owners and resetting the two explicitly retired runtime formats.
Run with the candidate interpreter, after the paired installed-path acceptance.
"""

import argparse
import fcntl
import json
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from agent_comms.active_route import (
    _publish_active_route_locked,
    active_route_path,
    read_active_route,
)
from agent_comms.cohort_foreground import _preflight
from agent_comms.comms import wire
from agent_comms.store_files import _atomic_write_text, _store_lock, file_revision

RUNTIME = Path.home() / ".local/share/agent-comms/runtime-failure-recovery-candidate-20260928"
NATIVE = (
    Path.home()
    / ".local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent"
)
LINKS = ("toad", "agent-comms", "agent-comms-acp", "agent-comms-agent", "agent-comms-nk-foreground")
RESET = (
    "compaction-commits.sqlite3",
    "compaction-commits.sqlite3-wal",
    "compaction-commits.sqlite3-shm",
    "compaction-commits.sqlite3-journal",
    "input_dispositions.json",
)


def require_no_clients():
    """Exclude old imported UI/ACP writers; never terminate an editor here."""
    clients = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if any(
            arg in (b"agent_comms.acp", b"toad", b"agent-comms-acp")
            or arg.endswith(b"/agent-comms-acp")
            or arg.endswith(b"/toad")
            for arg in arguments
        ):
            clients.append(int(entry.name))
    if clients:
        raise RuntimeError(f"Close Toad/ACP clients before runtime reset: {clients}")


def phase(comms, target):
    barrier = comms.owners.maintenance
    with _store_lock(barrier.wire_path), _store_lock(barrier.registry_path):
        receipt = barrier.current_unlocked()
        if receipt is None:
            raise RuntimeError("This operator requires the existing maintenance witness")
        data = dict(
            version=1, root=str(comms.root.resolve()), **asdict(replace(receipt, phase=target))
        )
        _atomic_write_text(barrier.state_path, json.dumps(data) + "\n", fsync_parent=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if Path(sys.prefix) != RUNTIME:
        raise RuntimeError("Run this operator with the installed candidate interpreter")
    route = read_active_route()
    if route is None:
        raise RuntimeError("No installed private route")
    _preflight(route.root, route.wire_root_id, NATIVE, True)
    comms = wire()
    if comms.root != route.root:
        raise RuntimeError("Environment selected a different root than the installed route")
    require_no_clients()
    owners = [t for t in comms.registry.snapshot().threads.values() if t.process_alive]
    for owner in owners:
        if owner.active_turn is not None:
            raise RuntimeError(f"Owner still in a turn: {owner.name}")
    previous_links = {}
    for name in LINKS:
        link = Path.home() / ".local/bin" / name
        if not link.is_symlink() or not (RUNTIME / "bin" / name).is_file():
            raise RuntimeError(f"Missing installed entry point: {name}")
        previous_links[name] = os.readlink(link)
    sessions = {
        t.session_file: file_revision(Path(t.session_file)) for t in owners if t.session_file
    }
    report = dict(
        runtime=str(RUNTIME),
        native=str(NATIVE),
        root=str(comms.root),
        owners=[t.name for t in owners],
        previous_links=previous_links,
        reset=[],
        replayed_inputs=0,
        applied=False,
    )
    if not args.apply:
        print(json.dumps(report, indent=2))
        return
    phase(comms, "draining")
    try:
        require_no_clients()
        for owner in owners:
            current = comms.registry.require(owner.name)
            if current.process_identity != owner.process_identity:
                raise RuntimeError(f"Owner changed before cutover: {owner.name}")
            if current.active_turn is not None:
                raise RuntimeError(f"Owner started work before admission closed: {owner.name}")
        for owner in owners:
            comms.owners.stop(owner.name)
        if any(t.process_alive for t in comms.registry.snapshot().threads.values()):
            raise RuntimeError("A writer survived retirement")
        require_no_clients()
        phase(comms, "installing")
        for name in RESET:
            path = comms.root / name
            if path.exists():
                report["reset"].append(dict(name=name, bytes=path.stat().st_size))
                path.unlink()
        directory = os.open(comms.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        replacement = replace(route, native_package=NATIVE)
        directory = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            _publish_active_route_locked(
                replacement, active_route_path(), directory, expected=route
            )
        finally:
            os.close(directory)
        for name in LINKS:
            link = Path.home() / ".local/bin" / name
            staged = link.with_name(name + ".paired-install")
            staged.symlink_to(RUNTIME / "bin" / name)
            staged.replace(link)
        comms.owners.pin_private_nk_launch(comms.root, route.wire_root_id, NATIVE)
        phase(comms, "ready")
        for owner in owners:
            comms.owners.start(owner.name)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if all(
                comms.owners._is_local_participant(comms.registry.require(t.name)) for t in owners
            ):
                break
            time.sleep(0.25)
        for owner in owners:
            current = comms.registry.require(owner.name)
            if not current.process_alive or not comms.owners._is_local_participant(current):
                raise RuntimeError(f"Fresh owner participation missing: {owner.name}")
            if (current.model, current.thinking_level, current.session_file) != (
                owner.model,
                owner.thinking_level,
                owner.session_file,
            ):
                raise RuntimeError(f"Owner configuration changed: {owner.name}")
            command = Path(f"/proc/{current.pid}/cmdline").read_bytes()
            if str(RUNTIME).encode() not in command:
                raise RuntimeError(f"Owner did not load candidate: {owner.name}")
        for name, before in sessions.items():
            if file_revision(Path(name)) != before:
                raise RuntimeError(f"Native history changed during quiet cutover: {name}")
        report["applied"] = True
    finally:
        report["maintenance"] = asdict(comms.owners.maintenance.read())
        _atomic_write_text(args.receipt, json.dumps(report, indent=2) + "\n", fsync_parent=True)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
