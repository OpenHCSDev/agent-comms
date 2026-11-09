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

from agent_comms.coordination.coordinator import Coordination
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.execution.owner_protocol import OwnerLine
from agent_comms.nominal.field_codec import FieldCodec, NullMember, SingleFieldValue, SubclassUnion
from agent_comms.registry.thread_identity import OwnerIdentity, ThreadIncarnation
from agent_comms.registry.threads import NativeThread, Thread
from agent_comms.storage.maintenance_barrier import MaintenanceHolder
from agent_comms.storage.private_path import PrivateSocketRole
from agent_comms.transport.jsonl_stream import JsonlStreamReader

if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.execution.child_process import ProcessIdentity
    from agent_comms.registry.registry_document import RegistryEntry
    from agent_comms.transport.runtime_request import RuntimeRequest


def socket_path(root: Path, pid: int) -> Path:
    return root / "runtime" / f"{pid}.sock"


class OwnerIdentityChangedError(RuntimeError, ConnectionError):
    """A saved attachment lost its root or thread identity and cannot reconnect."""


class OwnerReplyLostError(RuntimeError, ConnectionError):
    """The owner disconnected after dispatch; a mutating request remains uncertain."""


@dataclass(frozen=True, slots=True)
class NoControllerToken(NullMember):
    """The prompt came from no attached controller; no subscriber answers its dialogs."""


class RequestedOwner(SubclassUnion, affix="Owner"):
    """Which owner incarnation a request may bind: whichever holds its thread name now, or
    the one its caller captured. The bind checks it on the owner's side, so a name that
    acquires a new birth in the same process after the caller's check is refused."""

    @abstractmethod
    def require(self, owner: Thread) -> None: ...


class AnyOwner(NullMember, RequestedOwner):
    """An in-process caller binds the row that holds the name."""

    def require(self, owner: Thread) -> None:
        """Nothing was captured, so the current row is the one requested."""


@dataclass(frozen=True)
class CapturedOwner(SingleFieldValue, RequestedOwner):
    """A caller that checked one incarnation binds only that birth, under any of its names."""

    incarnation: ThreadIncarnation

    def require(self, owner: Thread) -> None:
        if owner.incarnation != self.incarnation:
            raise RelationViolationError("The owner incarnation changed before the request bound.")


class OwnerConnection:
    """A shared headless-client socket connection that checks root, owner and process identity."""

    @staticmethod
    def wire_request(
        action: str, owner: ThreadIncarnation, parameters: dict[str, object],
    ) -> dict[str, object]:
        """The owner decodes the command; transport encodes its one request envelope, bound
        to the incarnation the caller captured."""
        return {"action": action, "thread": owner.name,
                "captured": FieldCodec.encode(CapturedOwner(owner)),
                **cast(dict[str, object], FieldCodec.encode(parameters))}

    @classmethod
    def exchange(cls, root: Path, process: ProcessIdentity, request: RuntimeRequest) -> object:
        with cls.dispatching(root, process, request) as response:
            return cls.reply(response)

    @classmethod
    def exchange_at(cls, root: Path, owner: Thread, request: RuntimeRequest) -> object:
        """Ask this exact incarnation's owner process; the owner refuses a replacement that
        took the name after the caller read it."""
        return cls.exchange(root, owner.require_process(),
                            replace(request, captured=CapturedOwner(owner.incarnation)))

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
                try:
                    connection.connect(str(address))
                except FileNotFoundError as error:
                    # A live process with no runtime socket hosts no owner, such as the bridge
                    # behind an external CLI thread: the owner is unreachable, not a server fault.
                    raise ConnectionError(
                        f"Process {process.pid} serves no owner runtime socket") from error
                if not process.alive():
                    raise ConnectionError("Worker host exited before request dispatch")
                dispatched = replace(request, holder=MaintenanceHolder.current())
                connection.sendall((json.dumps(FieldCodec.encode(dispatched)) + "\n").encode())
                with connection.makefile("rb") as response:
                    yield response

    @staticmethod
    def reply(response: IO[bytes]) -> object:
        try:
            line = response.readline()
        except ConnectionResetError:
            # A host that dies with our request unread resets instead of closing.
            line = b""
        if not line:
            raise OwnerReplyLostError("Worker host disconnected after dispatch; outcome unknown")
        return OwnerLine.read(line).reply()

    def request_payload(self, action: str, parameters: dict[str, object]) -> dict[str, object]:
        return self.wire_request(action, self._identity, parameters)

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
        # Only a native owner serves a runtime socket; an external bridge with a live PID
        # has none, so it is refused here instead of waited on before dispatch.
        thread = comms.registry.require_member(owner.name, NativeThread)
        if thread.incarnation != owner:
            raise OwnerIdentityChangedError(
                "The owner incarnation changed while preparing the request."
            )
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
