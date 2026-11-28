"""Remote reads execute in worker processes, not under the UI's GIL."""

import os
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from functools import cached_property
from multiprocessing.connection import Connection
from threading import BoundedSemaphore, Lock
from typing import ClassVar, Self, cast

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.execution.child_process import Platform, ProcessName, WorkerStart
from agent_comms.execution.thread_launch import ThreadLaunch
from agent_comms.transport.remote_core import RemoteCoreAccess
from agent_comms.transport.request import ReadRequest, ToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.ui_model.observation_wire import EncodedConnection

_remote: RemoteCoreAccess
REMOTE_READ_PROCESS = ProcessName("ac-core-read")


def _initialize_remote(access: RemoteCoreAccess) -> None:
    """Each child retains its own TLS context and kept connection."""
    global _remote
    REMOTE_READ_PROCESS.publish()
    ThreadLaunch.strip(os.environ)
    _remote = access


def _read_remote[R: ToolResult](request: ReadRequest[R], generation: int = 0) -> R:
    # Select the transport implementation, not the parent's process dispatch.
    # The worker's profile still owns routing, credentials and retry semantics.
    _remote.connection_epoch.advance_to(generation)
    return RemoteCoreAccess.call(_remote, request)


class RemoteReadProcess:
    """One connection's bounded worker processes and accepted read lifetime.

    Thread callers wait for their typed result. Network and FieldCodec work
    execute in the workers; abandoning a waiter never replays the operation.
    Closing joins accepted reads before releasing their workers.
    """

    def __init__(self, access: RemoteCoreAccess, start: WorkerStart,
                 max_workers: int = 4) -> None:
        if max_workers < 2:
            raise ValueError("Remote reads need a separate observation decoding slot")
        self.workers = start.workers(max_workers, _initialize_remote, (access,))
        # A slow network cannot occupy every interpreter and strand an
        # already-arrived observation packet behind its own remote reads.
        self._read_slots = BoundedSemaphore(max_workers - 1)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def submit[T, **P](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        return self.workers.submit(fn, *args, **kwargs)

    def read[R: ToolResult](self, request: ReadRequest[R], generation: int = 0) -> R:
        with self._read_slots:
            return self.submit(_read_remote, request, generation).result()

    def decode[R](self, decode: Callable[[bytes], R], packet: bytes) -> R:
        return self.submit(decode, packet).result()

    def close(self) -> None:
        self.workers.shutdown(wait=True)


class ProcessViewerEnd(Connection):
    """Read bytes on the receiving worker; decode their records in a child."""

    def __init__(self, handle: int, process: RemoteReadProcess,
                 codec: type[EncodedConnection]) -> None:
        super().__init__(handle)
        self.process = process
        self.codec = codec

    def send(self, obj: object) -> None:
        self.send_bytes(self.codec.encode(obj))

    def recv(self) -> object:
        return self.process.decode(self.codec.decode, self.recv_bytes())


class ProcessRemoteCoreAccess(RemoteCoreAccess, MroDispatch):
    """Read capability selected by the request's nominal declaration.

    Writes and observed operations retain their original transport and progress
    owner. In particular, a lost write reply never becomes a process retry.
    The connection must close this resource after its acquired reads retire.
    """

    helper_start: ClassVar[WorkerStart] = Platform.current().worker_start()
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
        return self.read_process.read(request, self.connection_epoch.value)

    @handles(ToolRequest)
    def call_in_place[R: ToolResult](self, request: ToolRequest[R]) -> R:
        return RemoteCoreAccess.call(self, request)

    def viewer_end(self, handle: int, codec: type[EncodedConnection]) -> ProcessViewerEnd:
        return ProcessViewerEnd(handle, self.read_process, codec)

    def close(self) -> None:
        # An unused executor starts no process, including when closed here.
        self.read_process.close()
