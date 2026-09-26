"""Disposable Linux-only probe; NOT a production native Pi terminal receipt.

Run with ``python experiments/native_retirement_probe.py``. Only processes launched
by this invocation are signalled; the controller is an isolated subreaper.
"""

from __future__ import annotations

import array
import ctypes
import json
import os
import select
import signal
import socket
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36
PR_SET_PDEATHSIG = 1


def _arm_parent_death() -> None:
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot arm disposable parent-death signal")


def _report(kind: str) -> None:
    sock = socket.socket(fileno=int(os.environ["PROBE_FD"]))
    try:
        sock.send(json.dumps({"kind": kind, "pid": os.getpid(), "pgrp": os.getpgrp()}).encode())
    finally:
        sock.detach()  # keep the socket for the disposable descendant


def _bounded_fake_lifetime() -> None:
    # A child which escaped killpg still exits if the test controller dies or
    # exceeds this fixed lifetime; only disposable descendants are involved.
    controller = int(os.environ["PROBE_CONTROLLER_FD"])
    reader = select.poll()
    reader.register(controller, select.POLLIN)
    reader.poll(15_000)


def _tool() -> None:
    os.setsid()
    _report("tool")
    _bounded_fake_lifetime()


def _escape() -> None:
    os.setsid()
    _report("escaped")
    if os.environ.get("PROBE_SPAWN_TOOL") == "1":
        fd = int(os.environ["PROBE_FD"])
        controller_fd = int(os.environ["PROBE_CONTROLLER_FD"])
        subprocess.Popen(
            [sys.executable, str(Path(__file__).absolute()), "tool"],
            pass_fds=(fd, controller_fd),
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=None,
        )
    _bounded_fake_lifetime()


def _spawn_escape() -> subprocess.Popen[bytes]:
    fd = int(os.environ["PROBE_FD"])
    controller_fd = int(os.environ["PROBE_CONTROLLER_FD"])
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).absolute()), "escape"],
        pass_fds=(fd, controller_fd),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=None,
    )


def _namespace_init() -> None:
    if os.getpid() != 1:
        raise RuntimeError("namespace init must be PID 1")
    _arm_parent_death()
    _report("init")
    os.environ["PROBE_SPAWN_TOOL"] = "1"
    _spawn_escape()
    signal.pause()


def _group_leader() -> None:
    _report("leader")
    _spawn_escape()
    signal.pause()


def _recv(sock: socket.socket) -> tuple[str, int, int]:
    data, ancillary, _, _ = sock.recvmsg(4096, 256)
    decoded = json.loads(data)
    credentials = [
        (pid, uid, gid)
        for level, kind, payload in ancillary
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS
        for pid, uid, gid in [array.array("i", payload[:12])]
    ]
    if len(credentials) != 1:
        raise RuntimeError("no exact kernel peer credentials")
    host_pid, uid, _ = credentials[0]
    if uid not in (0, os.getuid()):
        raise RuntimeError("unexpected sender uid")
    return decoded["kind"], host_pid, decoded["pgrp"]


def _is_live(pidfd: int) -> bool:
    reader = select.poll()
    reader.register(pidfd, select.POLLIN)
    return not reader.poll(0)


def _kill(pidfd: int) -> None:
    with suppress(ProcessLookupError):
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)


def _controller() -> None:
    if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
        raise RuntimeError("Linux pidfds required")
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot isolate subreaper")
    summary: dict[str, object] = {}
    controller_fd = os.pidfd_open(os.getpid())
    for mode in ("group", "namespace"):
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        parent.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        parent.settimeout(5.0)
        env = os.environ.copy()
        env["PROBE_FD"] = str(child.fileno())
        env["PROBE_CONTROLLER_FD"] = str(controller_fd)
        command = (
            [sys.executable, str(Path(__file__).absolute()), "group-leader"]
            if mode == "group"
            else [
                "unshare",
                "--user",
                "--map-root-user",
                "--pid",
                "--fork",
                "--mount-proc",
                "--kill-child",
                "--",
                sys.executable,
                str(Path(__file__).absolute()),
                "namespace-init",
            ]
        )
        process = subprocess.Popen(
            command,
            pass_fds=(child.fileno(), controller_fd),
            start_new_session=(mode == "group"),
            preexec_fn=_arm_parent_death,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        child.close()
        pids: dict[str, int] = {}
        fds: dict[str, int] = {}
        try:
            for _ in range(2 if mode == "group" else 3):
                kind, host_pid, pgrp = _recv(parent)
                print(f"RECEIVED {mode} {kind} {host_pid}", file=sys.stderr, flush=True)
                if kind not in ("leader", "init", "escaped", "tool") or kind in pids:
                    raise RuntimeError("ambiguous child report")
                pids[kind] = host_pid
                fds[kind] = os.pidfd_open(host_pid)
                if mode == "namespace" and kind == "init":
                    nspid = next(
                        line.split()[1:]
                        for line in Path(f"/proc/{host_pid}/status").read_text().splitlines()
                        if line.startswith("NSpid:")
                    )
                    if len(nspid) < 2 or nspid[-1] != "1":
                        raise RuntimeError("init is not PID 1 in a new PID namespace")
                    summary["init_nspid"] = nspid
                if kind in ("escaped", "tool"):
                    summary[f"{mode}_{kind}_pgrp"] = pgrp
            escaped = pids["escaped"]
            if mode == "group":
                if pids["leader"] != process.pid:
                    raise RuntimeError("group leader is not the spawned child")
                if os.getpgid(escaped) == process.pid:
                    raise RuntimeError("fake child failed to escape the process group")
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
                summary["group_escape_survived_leader_kill"] = _is_live(fds["escaped"])
                if not summary["group_escape_survived_leader_kill"]:
                    raise RuntimeError("baseline did not expose killpg escape")
            else:
                namespace = os.readlink(f"/proc/{pids['init']}/ns/pid")
                for descendant in ("escaped", "tool"):
                    if os.getpgid(pids[descendant]) == os.getpgid(pids["init"]):
                        raise RuntimeError(f"fake {descendant} failed to escape init group")
                    if os.readlink(f"/proc/{pids[descendant]}/ns/pid") != namespace:
                        raise RuntimeError(f"fake {descendant} escaped the PID namespace")
                _kill(fds["init"])
                process.wait(timeout=3)
                for descendant in ("escaped", "tool"):
                    reader = select.poll()
                    reader.register(fds[descendant], select.POLLIN)
                    summary[f"namespace_{descendant}_retired_after_init_kill"] = bool(
                        reader.poll(3000)
                    )
                    if not summary[f"namespace_{descendant}_retired_after_init_kill"]:
                        raise RuntimeError(f"{descendant} survived PID namespace init death")
            # The local subreaper receives and reaps orphaned disposable fakes.
            for descendant in (("escaped",) if mode == "group" else ("escaped", "tool")):
                if _is_live(fds[descendant]):
                    _kill(fds[descendant])
                try:
                    _, status = os.waitpid(pids[descendant], 0)
                except ChildProcessError:
                    # On namespace teardown the kernel may reap the killed task
                    # without ever reparenting it as this subreaper's child.
                    if Path(f"/proc/{pids[descendant]}").exists():
                        raise RuntimeError(f"{descendant} exited but remains unreaped") from None
                    summary[f"{mode}_{descendant}_reaped_by_kernel"] = True
                else:
                    summary[f"{mode}_{descendant}_reaped_status"] = status
        finally:
            for fd in fds.values():
                _kill(fd)
                os.close(fd)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            parent.close()
            if process.stderr is not None:
                error = process.stderr.read(4096).decode(errors="replace")
                if error:
                    summary[f"{mode}_stderr"] = error
                    print(f"CHILD STDERR {mode}: {error}", file=sys.stderr, flush=True)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    modes = {
        "escape": _escape,
        "tool": _tool,
        "namespace-init": _namespace_init,
        "group-leader": _group_leader,
        "controller": _controller,
    }
    modes[sys.argv[1] if len(sys.argv) > 1 else "controller"]()
