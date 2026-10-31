"""Core owner-socket I/O and identity checks, independent of ACP presentation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import IO, TYPE_CHECKING, ClassVar, Self, cast

from .coordinator import Coordination
from .errors import RelationViolationError, UnregisteredThreadError
from .nominal.field_codec import FieldCodec, NullMember
from .jsonl_stream import JsonlStreamReader
from .maintenance_barrier import MaintenanceHolder
from .owner_protocol import OwnerLine
from .private_path import PrivateSocketRole
from .thread_identity import OwnerIdentity, ThreadIncarnation

if TYPE_CHECKING:
    from agent_comms.runtime_server import RuntimeRequest
    from .child_process import ProcessIdentity
    from .comms import Comms
    from .registry_document import RegistryEntry
    from .threads import Thread


def socket_path(root: Path, pid: int) -> Path:
    return root / "runtime" / f"{pid}.sock"


class OwnerIdentityChangedError(RuntimeError, ConnectionError):
    """A saved attachment lost its root or thread identity and cannot reconnect."""


class OwnerReplyLostError(RuntimeError, ConnectionError):
    """The owner disconnected after dispatch; a mutating request remains uncertain."""


@dataclass(frozen=True, slots=True)
class NoControllerToken(NullMember):
    """The prompt came from no attached controller; no subscriber answers its dialogs."""


class OwnerConnection:
    """A shared headless-client socket connection that checks root, owner and process identity."""

    @staticmethod
    def wire_request(action: str, thread: str, parameters: dict[str, object]) -> dict[str, object]:
        """The owner decodes the command; transport encodes its one request envelope."""
        return {"action": action, "thread": thread,
                **cast(dict[str, object], FieldCodec.encode(parameters))}

    @classmethod
    def exchange(cls, root: Path, process: ProcessIdentity, request: RuntimeRequest) -> object:
        with cls.dispatching(root, process, request) as response:
            return cls.reply(response)

    @staticmethod
    @contextmanager
    def dispatching(
        root: Path, process: ProcessIdentity, request: RuntimeRequest,
    ) -> Iterator[IO[bytes]]:
        """Dispatch once; the caller owns the response until the scope closes.

        Lifecycle calls already run on a coordination worker. Submitting their
        socket preparation back to that same executor would deadlock it.
        The launcher binds the inherited listener before publishing the host;
        there is no readiness polling and no retry after dispatch.
        """
        with PrivateSocketRole.address(socket_path(root, process.pid)) as address:
            with socket.socket(socket.AF_UNIX) as connection:
                if not process.alive():
                    raise ConnectionError("Worker host exited before request dispatch")
                connection.connect(str(address))
                if not process.alive():
                    raise ConnectionError("Worker host exited before request dispatch")
                dispatched = replace(request, holder=MaintenanceHolder.current())
                connection.sendall((json.dumps(FieldCodec.encode(dispatched)) + "\n").encode())
                with connection.makefile("rb") as response:
                    yield response

    @staticmethod
    def reply(response: IO[bytes]) -> object:
        line = response.readline()
        if not line:
            raise OwnerReplyLostError("Worker host disconnected after dispatch; outcome unknown")
        return OwnerLine.read(line).reply()

    def request_payload(self, action: str, parameters: dict[str, object]) -> dict[str, object]:
        return self.wire_request(action, self._identity.name, parameters)

    def __init__(self, comms: Comms, identity: ThreadIncarnation, path: Path):
        self._identity = identity
        self.path = path
        self._comms = comms
        self._closed = False
        self._requests: set[asyncio.Task] = set()
        self._root_identity = comms.root.stat()

    def require_identity(self, owner: ThreadIncarnation) -> None:
        """The incarnation this connection captured is the one the caller holds."""
        if self._identity != owner:
            raise OwnerIdentityChangedError(
                "The owner incarnation changed while preparing the request."
            )

    def _owner_entry(self) -> RegistryEntry:
        if self._closed:
            raise ConnectionError("Owner attachment was closed before request dispatch")
        try:
            same_root = os.path.samestat(self._root_identity, self._comms.root.stat())
        except FileNotFoundError:
            same_root = False
        if not same_root:
            raise OwnerIdentityChangedError("Wire root changed; open a new attachment.")
        try:
            entry = self._comms.registry.entry(self._identity.name).entry
        except UnregisteredThreadError as error:
            raise OwnerIdentityChangedError(
                "Thread identity changed; open a new attachment."
            ) from error
        if (not entry.matches_name(self._identity.name)
                or self._identity != entry.thread.incarnation):
            raise OwnerIdentityChangedError("Thread identity changed; open a new attachment.")
        return entry

    def _require_connect_owner(self, owner: OwnerIdentity, process: ProcessIdentity) -> None:
        try:
            self._owner_entry().require_owner_process(owner, process)
        except RelationViolationError as error:
            raise ConnectionError(f"Cannot attach {self._identity.name!r}: {error}") from error
        if not process.alive():
            raise ConnectionError(
                f"Owner {process.pid} for {self._identity.name!r} exited before attachment; "
                f"request not sent. Startup output: {self._comms.root / 'diagnostics'}"
            )

    async def _connect_current(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Startup has no elapsed-time expiry. The registered process and lease
        # own its lifetime; caller cancellation also ends this unsent operation.
        entry = await Coordination.run_worker(self._owner_entry)
        try:
            entry.status.require_active()
            process = entry.thread.require_process()
        except RelationViolationError as error:
            raise ConnectionError(f"Cannot attach {self._identity.name!r}: {error}") from error
        owner = entry.owner_identity
        path = socket_path(self._comms.root, process.pid)

        while True:
            await Coordination.run_worker(partial(self._require_connect_owner, owner, process))
            try:
                with PrivateSocketRole.address(path) as address:
                    reader, writer = await asyncio.open_unix_connection(address, limit=8 * 1024 * 1024)
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.05)
                continue
            try:
                await Coordination.run_worker(partial(self._require_connect_owner, owner, process))
                self.path = path
                return reader, writer
            except BaseException:
                writer.close()
                raise

    async def _read_response(self, reader: asyncio.StreamReader) -> bytes:
        return await JsonlStreamReader(reader).readline()

    async def request(self, action: str, **kwargs: object) -> object:
        if self._closed:
            raise ConnectionError("Owner attachment was closed before request dispatch")
        task = asyncio.current_task()
        assert task is not None
        self._requests.add(task)
        writer = None
        try:
            reader, writer = await self._connect_current()
            if self._closed:
                raise ConnectionError("Owner attachment was closed before request dispatch")
            writer.write(
                (
                    json.dumps(
                        self.request_payload(action, kwargs)
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            line = await self._read_response(reader)
            if not line:
                raise OwnerReplyLostError("Thread owner disconnected after request; outcome unknown.")
            return OwnerLine.read(line).reply()
        finally:
            try:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
            finally:
                self._requests.discard(task)

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._requests)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class OwnerReading[Result](ABC):
    """One exchange with exactly one live native owner incarnation over its socket."""

    def require_native_owner(self, owner: Thread) -> None:
        """Only a native owner serves a runtime socket; an external bridge with a live PID
        has none, so it is refused here instead of waited on before dispatch."""
        owner.execution.require_native()

    @abstractmethod
    async def read(self, owner: OwnerConnection) -> Result: ...


class OwnerReplyConnection(OwnerConnection):
    """Bound only the live owner's response after a request has been dispatched."""

    REPLY_TIMEOUT: ClassVar[float] = 3

    @classmethod
    def held(cls, comms: Comms, owner: ThreadIncarnation, reading: OwnerReading) -> Self:
        """A connection to exactly this registered native owner incarnation.

        The selected registration must be the held incarnation before its process is
        asked for; the connection's own capture is then checked again, since the
        registry may change between the two reads.
        """
        thread = comms.registry.require(owner.name)
        if thread.incarnation != owner:
            raise OwnerIdentityChangedError(
                "The owner incarnation changed while preparing the request."
            )
        reading.require_native_owner(thread)
        connection = cls(comms, owner, socket_path(comms.root, thread.require_process().pid))
        connection.require_identity(owner)
        return connection

    @classmethod
    async def answer[Result](
        cls,
        comms: Comms,
        owner: ThreadIncarnation,
        reading: OwnerReading[Result],
    ) -> Result:
        """One reading from the held owner; the connection closes whatever the outcome."""
        connection = await asyncio.to_thread(cls.held, comms, owner, reading)
        try:
            return await reading.read(connection)
        finally:
            await connection.close()

    async def _read_response(self, reader: asyncio.StreamReader) -> bytes:
        async with asyncio.timeout(self.REPLY_TIMEOUT):
            return await super()._read_response(reader)
