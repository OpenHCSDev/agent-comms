"""Core owner-socket I/O and identity checks, independent of ACP presentation."""

from __future__ import annotations

import asyncio
import json
import os
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .coordinator import Coordination
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import FieldCodec
from .jsonl_stream import JsonlStreamReader
from .owner_protocol import OwnerLine
from .private_path import PrivateSocketRole

if TYPE_CHECKING:
    from .child_process import ProcessIdentity
    from .registry_document import RegistryEntry
    from .thread_identity import OwnerIdentity


def socket_path(root: Path, pid: int) -> Path:
    return root / "runtime" / f"{pid}.sock"


class OwnerIdentityChangedError(RuntimeError, ConnectionError):
    """A saved attachment lost its root or thread identity and cannot reconnect."""


class OwnerReplyLostError(RuntimeError, ConnectionError):
    """The owner disconnected after dispatch; a mutating request remains uncertain."""


class OwnerConnection:
    """A shared headless-client socket connection that checks root, owner and process identity."""

    @staticmethod
    def wire_request(action: str, thread: str, parameters: dict[str, object]) -> dict[str, object]:
        """The owner decodes the command; transport encodes its one request envelope."""
        return {"action": action, "thread": thread, **FieldCodec.encode(parameters)}

    def request_payload(self, action: str, parameters: dict[str, object]) -> dict[str, object]:
        return self.wire_request(action, self.session_id, parameters)

    def __init__(self, comms, session_id: str, path: Path):
        self.session_id = session_id
        self.path = path
        self._comms = comms
        self._closed = False
        self._requests: set[asyncio.Task] = set()
        self._identity = comms.registry.entry(session_id).entry.thread.incarnation
        self._root_identity = comms.root.stat()

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
            entry = self._comms.registry.entry(self.session_id).entry
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
            raise ConnectionError(f"Cannot attach {self.session_id!r}: {error}") from error
        if not process.alive():
            raise ConnectionError(
                f"Owner {process.pid} for {self.session_id!r} exited before attachment; "
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
            raise ConnectionError(f"Cannot attach {self.session_id!r}: {error}") from error
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

    async def close(self):
        self._closed = True
        tasks = tuple(self._requests)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

