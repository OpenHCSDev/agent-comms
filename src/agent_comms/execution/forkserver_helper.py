"""Preloaded forkserver helper start for long-lived POSIX servers.

Kept separate from ``child_process`` because ``multiprocessing.forkserver`` imports
``_multiprocessing`` at module load time, which Android's Python build omits (bionic has no
POSIX named semaphores).
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import threading
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing import forkserver, util
from typing import Protocol, cast

from agent_comms.execution.child_process import HelperStart


class _ForkServerState(Protocol):
    _lock: threading.Lock
    _forkserver_pid: int | None
    _forkserver_alive_fd: int | None
    _forkserver_address: str | bytes | None
    _forkserver_authkey: bytes | None


@dataclass(frozen=True)
class ForkserverHelper(HelperStart):
    """Helpers forked from a single-threaded forkserver that preloads its modules once.

    A long-lived server (such as the connector serving remote ``/observe`` sockets) cannot
    fork from itself once its event loop and thread pool are running, yet spawning a fresh
    interpreter per reconnect pays the full import and schema-discovery graph on every drop.
    A forkserver imports ``preload`` once and forks each helper warm; ``retire`` closes its
    alive pipe and stops a still-preloading forkserver so server shutdown never waits out an
    in-flight import.
    """

    preload: tuple[str, ...] = ()

    def context(self) -> multiprocessing.context.ForkServerContext:
        context = multiprocessing.get_context("forkserver")
        context.set_forkserver_preload(list(self.preload))
        return context

    def prepare(self) -> None:
        super().prepare()
        if os.name == "posix":
            self.context()
            forkserver.ensure_running()

    def retire(self) -> None:
        if os.name != "posix":
            return
        server = cast(_ForkServerState, forkserver._forkserver)
        with server._lock:
            pid = server._forkserver_pid
            if pid is None:
                return
            alive_fd = server._forkserver_alive_fd
            address = server._forkserver_address
            server._forkserver_alive_fd = None
            server._forkserver_pid = None
            server._forkserver_address = None
            server._forkserver_authkey = None
        if alive_fd is not None:
            os.close(alive_fd)
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == 0:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            with suppress(ChildProcessError):
                os.waitpid(pid, 0)
        if address is not None and not util.is_abstract_socket_namespace(address):
            with suppress(FileNotFoundError):
                os.unlink(address)
