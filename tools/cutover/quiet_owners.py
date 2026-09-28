"""One-shot quiet stop with OLD runtime; explicit restart with NEW runtime.

Parent owns D22 and route/launcher publication between these separate invocations.
No input submission, UNKNOWN retirement, process-group signal or forced kill here.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import shlex
import signal
import sys
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4


def pidfd_open(pid: int) -> int:
    # The configured standalone Python omits os.pidfd_open; the host libc owns
    # the same Linux API, avoiding PID reuse without changing interpreters.
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.pidfd_open
    call.argtypes, call.restype = (ctypes.c_int, ctypes.c_uint), ctypes.c_int
    descriptor = call(pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return descriptor


def terminate_exact(descriptor: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.pidfd_send_signal
    call.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    if call(descriptor, signal.SIGTERM, None, 0) < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


def process_identity(pid: int) -> tuple[str, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = raw[raw.rfind(") ") + 2 :].split()
    return fields[0], int(fields[19])


def assert_no_clients(root: Path, allowed: frozenset[int]) -> None:
    """Refuse user interfaces and non-owner root users; never signal them."""
    from agent_comms.active_route import read_active_route

    active = read_active_route()
    default_root = active is not None and active.root.resolve() == root
    blockers = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == os.getpid() or pid in allowed:
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
            identity = process_identity(pid)
            if identity is None or identity[0] == "Z":
                continue
            name = (entry / "comm").read_text().strip()
            command = (entry / "cmdline").read_bytes().split(b"\0")
            is_client = name == "toad" or any(
                token in {b"agent_comms.acp", b"agent_comms.worker"}
                or Path(os.fsdecode(token)).name in {"toad", "agent-comms-acp", "agent-comms-agent"}
                for token in command
                if token
            )
            try:
                environment = dict(
                    item.split(b"=", 1)
                    for item in (entry / "environ").read_bytes().split(b"\0")
                    if b"=" in item
                )
            except PermissionError:
                if is_client:
                    blockers.append(f"Cannot inspect Comms client {pid}; do not stop it")
                continue
            configured = environment.get(b"AGENT_COMMS_ROOT")
            selected = Path(os.fsdecode(configured)).resolve() if configured else None
            if selected == root or (is_client and selected is None and default_root):
                blockers.append(f"Client {pid} ({name}) is still attached; leave it running")
                continue
            try:
                for fd in (entry / "fd").iterdir():
                    try:
                        target = Path(os.readlink(fd))
                    except FileNotFoundError:
                        continue
                    if target.is_absolute() and target.is_relative_to(root):
                        blockers.append(f"Process {pid} has root files open; leave it running")
                        break
            except PermissionError:
                if is_client and selected in (None, root):
                    blockers.append(f"Cannot inspect client {pid}; leave it running")
                # Protected unrelated services do not acquire Comms authority.
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue
    if blockers:
        raise RuntimeError("; ".join(blockers))


def configured_runtime(runtime: Path, route_path: Path | None):
    if sys.platform != "linux":
        raise RuntimeError("This operator requires Linux pidfd support")
    if not sys.flags.isolated or Path(sys.prefix).resolve() != runtime.resolve():
        raise RuntimeError("Use the explicitly configured runtime/bin/python -I")
    import agent_comms

    if not Path(agent_comms.__file__).resolve().is_relative_to(runtime.resolve()):
        raise RuntimeError("Imported agent_comms is outside the configured installed runtime")
    from agent_comms.active_route import read_active_route

    route = read_active_route(route_path)
    if route is None:
        raise RuntimeError("An explicit installed active route is required")
    return route


def write_json(path: Path, value) -> None:
    from agent_comms.store_files import _atomic_write_text

    _atomic_write_text(path, json.dumps(value, indent=2), fsync_parent=True)


def session_revision(path: str) -> tuple[int, int, int, int]:
    info = Path(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def stop(args) -> None:
    route = configured_runtime(args.runtime, args.route_file)
    root = args.root.resolve()
    if route.root.resolve() != root:
        raise RuntimeError("Active route changed; refusing a different root")
    if args.receipt.exists() or args.receipt.resolve().is_relative_to(root):
        raise RuntimeError("Receipt must be a NEW JSON path outside the live root")
    from agent_comms.comms import Comms
    from agent_comms.store_files import _store_lock

    comms = Comms(root)
    owners = comms.owners
    with ExitStack() as handles:
        with _store_lock(root / "wire"):
            owners.maintenance.assert_open_unlocked()
            snapshot = comms.registry.snapshot()
            if any(thread.active_turn is not None for thread in snapshot.threads.values()):
                raise RuntimeError("An active turn exists; no owner will be fenced")
            selected = tuple(
                thread
                for thread in snapshot.threads.values()
                if thread.role.executable and thread.pid > 0 and owners._process_alive(thread.pid)
            )
            allowed = frozenset(thread.pid for thread in selected)
            assert_no_clients(root, allowed)
            captured = []
            receipt = {
                "root": str(root),
                "wire_root_id": route.wire_root_id,
                "runtime": str(args.runtime.resolve()),
                "owners": {},
                "stopped": [],
                "phase": "preflight",
            }
            for thread in selected:
                if not snapshot.statuses[thread.name].active:
                    raise RuntimeError(f"Owner {thread.name} is already fenced; inspect it")
                if thread.pid == os.getpid() or not owners._is_local_participant(
                    thread, wait=False
                ):
                    raise RuntimeError(f"Cannot prove owner {thread.name}")
                proc = Path(f"/proc/{thread.pid}")
                command = (proc / "cmdline").read_bytes().split(b"\0")
                if b"agent_comms.worker" not in command:
                    raise RuntimeError(
                        f"{thread.name} is not a detached worker; no UI can be stopped"
                    )
                identity = process_identity(thread.pid)
                if identity is None or identity[0] == "Z":
                    raise RuntimeError(f"Owner {thread.name} exited before capture")
                descriptor = pidfd_open(thread.pid)
                handles.callback(os.close, descriptor)
                captured_identity = process_identity(thread.pid)
                if captured_identity is None or captured_identity[1] != identity[1]:
                    raise RuntimeError(f"Process identity changed for {thread.name}")
                environment = dict(
                    item.split(b"=", 1)
                    for item in (proc / "environ").read_bytes().split(b"\0")
                    if b"=" in item
                )
                declaration = {
                    "pid": thread.pid,
                    "start_ticks": identity[1],
                    "created_at": thread.created_at,
                    "worktree": thread.worktree,
                    "model": thread.model,
                    "thinking_level": thread.thinking_level,
                    "session_file": thread.session_file,
                    "admission_generation": snapshot.admission_generations[thread.name],
                }
                for field, variable in (
                    ("agent_bin", b"AGENT_COMMS_AGENT_BIN"),
                    ("agent_args", b"AGENT_COMMS_AGENT_ARGS"),
                ):
                    if variable in environment:
                        value = os.fsdecode(environment[variable])
                        declaration[field] = shlex.split(value) if field == "agent_args" else value
                receipt["owners"][thread.name] = declaration
                captured.append((thread, identity[1], descriptor))
            sessions = {
                thread.session_file: session_revision(thread.session_file)
                for thread in selected
                if thread.session_file
            }
            input_path = root / "input_dispositions.json"
            original_inputs = input_path.read_bytes() if input_path.exists() else None
            receipt["input_outcomes"] = json.loads(original_inputs) if original_inputs else None
            receipt["native_sessions"] = sessions
            if args.dry_run:
                print(json.dumps(receipt, indent=2))
                return
            # One existing RegistryEdit commits all exact idle fences together.
            # A crash after publishing admission closure stays closed; no rollback
            # clears a fence or rewrites an UNKNOWN input.
            with comms.registry.store.editing() as edit:
                if any(thread.active_turn is not None for thread in edit.document.threads.values()):
                    raise RuntimeError("Owner became busy; no owner will be fenced")
                for thread, _ticks, _descriptor in captured:
                    receipt["owners"][thread.name]["fenced_admission_generation"] = (
                        edit.document.fence_idle_owner(
                            thread,
                            expected_admission_generation=snapshot.admission_generations[
                                thread.name
                            ],
                        )
                    )
                assert_no_clients(root, allowed)
                current_gate = owners.maintenance.current_unlocked()
                generation = current_gate.generation + 1 if current_gate else 1
                nonce = uuid4().hex
                receipt["maintenance"] = {"generation": generation, "nonce": nonce}
                write_json(args.receipt, receipt)
                write_json(
                    owners.maintenance.marker_path,
                    {
                        "version": 1,
                        "root": str(root),
                        "generation": generation,
                    },
                )
                write_json(
                    owners.maintenance.state_path,
                    {
                        "version": 1,
                        "root": str(root),
                        "generation": generation,
                        "operator": "d22-quiet-owners",
                        "nonce": nonce,
                        "phase": "installing",
                    },
                )
                edit.commit()
            receipt["phase"] = "fenced"
            write_json(args.receipt, receipt)
            assert_no_clients(root, allowed)
            current = comms.registry.snapshot()
            for thread, ticks, descriptor in captured:
                identity = process_identity(thread.pid)
                expected = receipt["owners"][thread.name]["fenced_admission_generation"]
                if (
                    current.threads[thread.name] != thread
                    or not current.statuses[thread.name].stopped
                    or current.admission_generations[thread.name] != expected
                    or identity is None
                    or identity[1] != ticks
                ):
                    raise RuntimeError(f"Fenced owner changed: {thread.name}")
                terminate_exact(descriptor)
        for thread, _ticks, descriptor in captured:
            poll = select.poll()
            poll.register(descriptor, select.POLLIN)
            if not poll.poll(5000):
                raise RuntimeError(f"Owner {thread.name} has not exited; no forced kill or restart")
            receipt["stopped"].append(thread.name)
            write_json(args.receipt, receipt)
        with _store_lock(root / "wire"):
            assert_no_clients(root, frozenset())
            current = comms.registry.snapshot()
            for thread, _ticks, _descriptor in captured:
                expected = receipt["owners"][thread.name]["fenced_admission_generation"]
                unchanged_fence = (
                    current.threads[thread.name] == thread
                    and current.statuses[thread.name].stopped
                    and current.admission_generations[thread.name] == expected
                )
                if not unchanged_fence and not owners._released_same_owner(
                    current, thread, expected
                ):
                    raise RuntimeError(f"Stopped declaration changed: {thread.name}")
            if (input_path.read_bytes() if input_path.exists() else None) != original_inputs:
                raise RuntimeError("Original input outcomes changed; inspect, do not replay")
            if {name: session_revision(name) for name in sessions} != sessions:
                raise RuntimeError("Native session changed during stop; preserve and inspect")
            with comms.bus.log.locked():
                receipt["source_highwater"] = comms.bus.log._private_marker_unlocked().last_seq
            receipt["phase"] = "stopped"
            write_json(args.receipt, receipt)
    print(json.dumps(receipt, indent=2))


def restart(args) -> None:
    route = configured_runtime(args.runtime, args.route_file)
    from agent_comms.comms import Comms
    from agent_comms.field_codec import FieldCodec
    from agent_comms.input_disposition import InputDocument
    from agent_comms.native_pi import _trusted_package
    from agent_comms.store_files import _store_lock

    saved = json.loads(args.receipt.read_text())
    root = args.root.resolve()
    if (
        saved["phase"] != "stopped"
        or saved["stopped"] != list(saved["owners"])
        or saved["root"] != str(root)
        or route.root.resolve() != root
        or route.wire_root_id != saved["wire_root_id"]
        or args.runtime.resolve() == Path(saved["runtime"])
        or route.native_package.resolve() != args.native_package.resolve()
    ):
        raise RuntimeError("Completed stop and new runtime/root/native pins must match")
    _trusted_package(route.native_package)
    if not (route.native_package / "dist/core/session-entry-store.js").is_file():
        raise RuntimeError("New native session-entry-store package is not installed")
    assert_no_clients(root, frozenset())
    comms = Comms(root)
    comms.owners.pin_private_nk_launch(root, route.wire_root_id, route.native_package)
    with _store_lock(root / "wire"), comms.registry.store.editing() as edit:
        gate = comms.owners.maintenance.current_unlocked()
        expected = saved["maintenance"]
        if gate is None or (gate.nonce, gate.generation, gate.phase) != (
            expected["nonce"],
            expected["generation"],
            "installing",
        ):
            raise RuntimeError("Cutover admission witness changed")
        with comms.bus.log.locked():
            marker = comms.bus.log._private_marker_unlocked()
            if marker.admission_after_seq < saved["source_highwater"]:
                raise RuntimeError("D22 did not exclude old bus rows from fresh admission")
        for name, owner in saved["owners"].items():
            current = edit.document.threads[name]
            if (
                current.created_at != owner["created_at"]
                or current.worktree != owner["worktree"]
                or current.session_file != owner["session_file"]
                or current.model != owner["model"]
                or current.thinking_level != owner["thinking_level"]
                or current.active_turn is not None
                or current.process_identity is not None
            ):
                raise RuntimeError(f"Migrated owner/session changed: {name}")
        for name, revision in saved["native_sessions"].items():
            actual = session_revision(name)
            # D22 copies internal sessions, preserving bytes/mtime while changing
            # their inode. External native sessions must remain the same inode.
            if actual[2:] != tuple(revision[2:]) or (
                not Path(name).is_relative_to(root) and actual != tuple(revision)
            ):
                raise RuntimeError(f"Saved native session changed: {name}")
        input_path = root / "input_dispositions.json"
        inputs = FieldCodec.decode(InputDocument, json.loads(input_path.read_text()))
        original = (
            FieldCodec.decode(InputDocument, saved["input_outcomes"])
            if saved["input_outcomes"] is not None
            else InputDocument()
        )
        if inputs != original:
            raise RuntimeError("Original input outcomes differ; do not recover/replay")
        if args.dry_run:
            print(
                json.dumps(
                    {"restart_preflight": list(saved["owners"]), "runtime": str(args.runtime)}
                )
            )
            return
        state = json.loads(comms.owners.maintenance.state_path.read_text())
        state["phase"] = "ready"
        write_json(comms.owners.maintenance.state_path, state)
    saved["restart_runtime"] = str(args.runtime.resolve())
    saved["restart_requested"] = []
    saved["phase"] = "restarting"
    write_json(args.receipt, saved)
    for name in saved["owners"]:
        # Do not reuse old absolute agent_bin from the receipt. The current
        # lifecycle maps default pi to this NEW interpreter's native launcher.
        result = comms.owners.start(
            name, agent_bin="pi", agent_args=saved["owners"][name].get("agent_args", [])
        )
        saved["restart_requested"].append({"name": result.thread, "pid": result.pid})
        write_json(args.receipt, saved)
    saved["phase"] = "restart_requested"  # reservations, not a native/model readiness claim
    write_json(args.receipt, saved)
    print(json.dumps(saved, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restart", action="store_true", help="After parent D22 and pin activation"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preflight only; no fences/signals/start"
    )
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--native-package", type=Path, help="Required new active pin for --restart")
    parser.add_argument(
        "--route-file", type=Path, help="Owned fixture route; default is installed route"
    )
    args = parser.parse_args()
    if args.restart and args.native_package is None:
        parser.error("--restart requires --native-package")
    (restart if args.restart else stop)(args)


if __name__ == "__main__":
    main()
