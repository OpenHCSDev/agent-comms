"""External, durable, incarnation-guarded restart queue for a reviewed Pi prompt.

PLAN records only live executable owners whose cwd has the exact reviewed prompt.
RUN is a separate explicit activation. Linux inotify wakes it on registry writes;
there is no deadline, polling, replay, or interruption of active turns. Run with
this worktree's src/ on PYTHONPATH until the guard is included in the package.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

from agent_comms.declarations import RelationViolationError
from agent_comms.operations import wire


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _policy_matches(worktree: str, digest: str) -> bool:
    path = Path(worktree) / ".pi" / "APPEND_SYSTEM.md"
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest


def plan(root: Path, queue: Path, prompt: Path) -> dict:
    if queue.exists():
        raise FileExistsError(f"Refusing to replace existing restart queue: {queue}")
    digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    comms = wire(root)
    snapshot = comms.registry.snapshot()
    entries = []
    excluded = []
    for thread in snapshot.threads.values():
        status = snapshot.statuses[thread.name]
        if not (thread.role.executable and status.active):
            continue
        if thread.pid <= 0 or not comms._process_alive(thread.pid):
            excluded.append({"name": thread.name, "reason": "no local live owner pid"})
            continue
        epoch = snapshot.admission_generations.get(thread.name)
        if epoch is None or not _policy_matches(thread.worktree, digest):
            excluded.append({"name": thread.name, "reason": "missing incarnation or prompt"})
            continue
        entries.append(
            {
                "name": thread.name,
                "pid": thread.pid,
                "created_at": thread.created_at,
                "epoch": epoch,
                "worktree": thread.worktree,
                "policy_sha256": digest,
                "state": "pending",
            }
        )
    payload = {"version": 1, "root": str(root.resolve()), "entries": entries, "excluded": excluded}
    _atomic_json(queue, payload)
    return payload


def _owner_environment(pid: int) -> tuple[dict[str, str], str]:
    """Inspect only the still-live verified owner; never journal its secrets."""
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    values = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    env = {os.fsdecode(key): os.fsdecode(value) for key, value in values.items()}
    argv0 = os.fsdecode(Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0])
    if not argv0 or not os.path.samefile(argv0, sys.executable):
        raise ValueError("Runner interpreter differs from original owner interpreter")
    if not env.get("AGENT_COMMS_AGENT_BIN"):
        raise ValueError("Original owner agent binary is unavailable")
    if env.get("AGENT_COMMS_THREAD") is None:
        raise ValueError("Original process lacks managed thread identity")
    return env, argv0


def _restart_exact_owner(comms, thread, expected):
    # Reprove before/after reading /proc: a reused pid cannot supply launch
    # environment without also passing the later locked incarnation/socket CAS.
    if not comms._is_local_participant(thread, wait=False):
        raise ValueError("Original owner lacks live socket proof")
    original, _argv0 = _owner_environment(thread.pid)
    if original["AGENT_COMMS_THREAD"] != thread.name:
        raise ValueError("Original owner environment names a different thread")
    if not comms._is_local_participant(thread, wait=False):
        raise ValueError("Original owner changed during environment read")
    saved = os.environ.copy()
    try:
        # _launch_owner_unlocked copies os.environ; preserve original worker
        # PYTHONPATH, native binary, model flags *presence*, and credentials,
        # never the isolated scheduler's PYTHONPATH/model/provider settings.
        os.environ.clear()
        os.environ.update(original)
        return comms.restart_owners(
            [thread.name],
            agent_bin=original["AGENT_COMMS_AGENT_BIN"],
            agent_args=(
                shlex.split(original["AGENT_COMMS_AGENT_ARGS"])
                if "AGENT_COMMS_AGENT_ARGS" in original
                else None
            ),
            expected_incarnations={thread.name: expected},
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)


def step(queue: Path) -> dict:
    data = json.loads(queue.read_text())
    if data.get("version") != 1:
        raise ValueError("Unrecognized restart queue version")
    comms = wire(data["root"])
    for entry in data["entries"]:
        if entry["state"] != "pending":
            continue
        snapshot = comms.registry.snapshot()
        name = entry["name"]
        thread = snapshot.threads.get(name)
        expected = (entry["pid"], entry["created_at"], entry["epoch"])
        observed = (
            (thread.pid, thread.created_at, snapshot.admission_generations.get(name))
            if thread
            else None
        )
        if observed != expected or not snapshot.statuses[name].active:
            entry.update(
                state="stale", reason="owner incarnation or status changed; no restart attempted"
            )
        elif thread.worktree != entry["worktree"] or not _policy_matches(
            thread.worktree, entry["policy_sha256"]
        ):
            entry.update(state="blocked", reason="worktree or project prompt changed")
        elif thread.active_turn is not None:
            continue
        else:
            try:
                (result,) = _restart_exact_owner(comms, thread, expected)
            except RelationViolationError as exc:
                # This API can fail after signaling; absent an explicit early
                # preflight reason, record UNKNOWN and stop rather than retry.
                reason = str(exc)
                if "became busy before restart" in reason:
                    continue
                if reason == "Idle owner changed before restart fence.":
                    fresh = comms.registry.snapshot()
                    current = fresh.threads.get(name)
                    if (
                        current is not None
                        and current.active_turn is not None
                        and (current.pid, current.created_at, fresh.admission_generations.get(name))
                        == expected
                    ):
                        continue  # A direct claim won; try only after its turn settles.
                early = (
                    "Queued owner incarnation changed before restart.",
                    "Guarded restart requires exactly one queued owner.",
                    "Idle owner changed before restart fence.",
                    "Owner selection changed before restart.",
                    "Owner epochs changed before restart.",
                )
                entry.update(state="blocked" if reason in early else "uncertain", reason=reason)
            except ValueError as exc:
                if "active turn" in str(exc):
                    continue
                entry.update(state="uncertain", reason=str(exc))
            except Exception as exc:
                entry.update(state="uncertain", reason=f"{type(exc).__name__}: {exc}")
            else:
                entry.update(state="restarted", old_pid=result.previous_pid, new_pid=result.pid)
        _atomic_json(queue, data)
        if entry["state"] == "uncertain":
            raise RuntimeError(
                f"Owner {name!r} restart outcome UNKNOWN; review queue before rerunning"
            )
    return data


def watch(root: Path):
    libc = ctypes.CDLL(None, use_errno=True)
    init = libc.inotify_init1
    init.argtypes = [ctypes.c_int]
    init.restype = ctypes.c_int
    add = libc.inotify_add_watch
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    add.restype = ctypes.c_int
    fd = init(os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    # Registry persistence uses replace/close; observe both without a timer.
    if add(fd, os.fsencode(root), 0x00000080 | 0x00000008) < 0:
        error = ctypes.get_errno()
        os.close(fd)
        raise OSError(error, "inotify_add_watch")
    try:
        yield fd
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "run"])
    parser.add_argument("--root", type=Path, default=Path("/home/ts/.agent-comms"))
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--prompt", type=Path)
    args = parser.parse_args()
    if args.action == "plan":
        if args.prompt is None:
            parser.error("plan requires --prompt")
        result = plan(args.root, args.queue, args.prompt)
    else:
        if not os.path.samefile(sys.executable, args.root / "stack/.venv/bin/python"):
            parser.error("run requires the production stack Python interpreter")
        # Install watch before the first step to avoid missing a terminal turn.
        from contextlib import contextmanager

        with contextmanager(watch)(args.root) as fd:
            while True:
                result = step(args.queue)
                if not any(entry["state"] == "pending" for entry in result["entries"]):
                    break
                os.read(fd, 65536)
    print(
        json.dumps(
            {
                "entries": len(result["entries"]),
                "states": {
                    state: sum(entry["state"] == state for entry in result["entries"])
                    for state in ("pending", "restarted", "stale", "blocked", "uncertain")
                },
                "excluded": len(result["excluded"]),
            }
        )
    )


if __name__ == "__main__":
    main()
