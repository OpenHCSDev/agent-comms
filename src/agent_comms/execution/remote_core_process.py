"""Remote reads execute in child interpreters, not under the UI's GIL."""

import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from functools import cached_property
from threading import BoundedSemaphore, Lock
from typing import ClassVar, cast

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.execution.child_process import HelperStart, SpawnedHelper
from agent_comms.execution.launch_runtime import ThreadLaunch
from agent_comms.transport.remote_core import RemoteCoreAccess
from agent_comms.transport.request import ReadRequest, ToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.ui_model.observation import ViewerEnd

_remote: RemoteCoreAccess


def _initialize_remote(access: RemoteCoreAccess) -> None:
    """Each child retains its own TLS context and kept connection."""
    global _remote
    ThreadLaunch.strip(os.environ)
    _remote = access


def _read_remote[R: ToolResult](request: ReadRequest[R]) -> R:
    # Select the transport implementation, not the parent's process dispatch.
    # The worker's profile still owns routing, credentials and retry semantics.
    return RemoteCoreAccess.call(_remote, request)


class RemoteReadProcess(ProcessPoolExecutor):
    """One connection's bounded child interpreters and accepted read lifetime.

    Thread callers wait for their typed result. Network and FieldCodec work
    execute in the children; abandoning a waiter never replays the operation.
    Closing joins accepted reads before releasing their workers.
    """

    def __init__(self, access: RemoteCoreAccess, start: HelperStart,
                 max_workers: int = 4) -> None:
        if max_workers < 2:
            raise ValueError("Remote reads need a separate observation decoding slot")
        super().__init__(max_workers=max_workers, mp_context=start.context(),
                         initializer=_initialize_remote, initargs=(access,))
        # A slow network cannot occupy every interpreter and strand an
        # already-arrived observation packet behind its own remote reads.
        self._read_slots = BoundedSemaphore(max_workers - 1)

    def read[R: ToolResult](self, request: ReadRequest[R]) -> R:
        with self._read_slots:
            return self.submit(_read_remote, request).result()

    def decode[R](self, decode: Callable[[bytes], R], packet: bytes) -> R:
        return self.submit(decode, packet).result()

    def close(self) -> None:
        self.shutdown(wait=True)


class ProcessViewerEnd(ViewerEnd):
    """Read bytes on the receiving worker; decode their records in a child."""

    def __init__(self, handle: int, process: RemoteReadProcess) -> None:
        super().__init__(handle)
        self.process = process

    def recv(self) -> object:
        return self.process.decode(type(self).decode, self.recv_bytes())


class ProcessRemoteCoreAccess(RemoteCoreAccess, MroDispatch):
    """Read capability selected by the request's nominal declaration.

    Writes and observed operations retain their original transport and progress
    owner. In particular, a lost write reply never becomes a process retry.
    The connection must close this resource after its acquired reads retire.
    """

    helper_start: ClassVar[HelperStart] = SpawnedHelper()
    _creation_lock: ClassVar[Lock] = Lock()

    @property
    def read_process(self) -> RemoteReadProcess:
        # cached_property alone permits concurrent construction on Python 3.12+.
        with self._creation_lock:
            return self._read_process

    @cached_property
    def _read_process(self) -> RemoteReadProcess:
        # Only declared profile fields cross spawn, never cached sockets, locks
        # or the live executor. A worker keeps this fresh profile between reads.
        return RemoteReadProcess(replace(self), self.helper_start)

    def call[R: ToolResult](self, request: ToolRequest[R]) -> R:
        return cast(R, type(self).handler_for(type(request))(self, request))

    @handles(ReadRequest)
    def read_in_process[R: ToolResult](self, request: ReadRequest[R]) -> R:
        return self.read_process.read(request)

    @handles(ToolRequest)
    def call_in_place[R: ToolResult](self, request: ToolRequest[R]) -> R:
        return RemoteCoreAccess.call(self, request)

    def viewer_end(self, handle: int) -> ProcessViewerEnd:
        return ProcessViewerEnd(handle, self.read_process)

    def close(self) -> None:
        # An unused executor starts no process, including when closed here.
        self.read_process.close()
