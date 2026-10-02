"""Bounded read-only Unix-socket recovery viewer for a post-paint UI task.

This module never opens coordinator SQLite, a bus file, or a Pi session. The
same-UID gateway authenticates only the local OS principal, NOT a human or an
agent thread. A caller must schedule this off the first-paint critical path;
its result is a disposable, redacted projection and cannot authorize Retry.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from .field_codec import FieldCodec
from .private_path import PrivateDirectoryRole, PrivateSocketRole, TrustedAncestorRole
from .recovery_projection import AvailableRecoveryProjection, RecoveryProjection, RecoveryRequest, UnavailableRecoveryProjection

_MAX_REPLY = 4096
_TIMEOUT = 0.75
_UNAVAILABLE = FieldCodec.encode(UnavailableRecoveryProjection("gateway_unavailable"))


def _valid_projection(value: object, thread: str) -> bool:
    try:
        projection = FieldCodec.decode(RecoveryProjection, value)
    except (TypeError, ValueError):
        return False
    return not isinstance(projection, AvailableRecoveryProjection) or projection.owner == thread


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("gateway response repeated a JSON key")
        result[key] = value
    return result


def _private_socket(path: Path) -> bool:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.name != "gateway.sock"
        or path.parent.name != ".recovery-viewer"
    ):
        return False
    try:
        root = path.parent.parent
        for ancestor in (root, *root.parents):
            info = ancestor.lstat()
            TrustedAncestorRole.require(info)
        PrivateDirectoryRole.require(root.lstat())
        PrivateDirectoryRole.require(path.parent.lstat())
        PrivateSocketRole.require(path.lstat())
        return True
    except (OSError, ValueError):
        return False


def _same_uid(writer: asyncio.StreamWriter) -> bool:
    peer = writer.get_extra_info("socket")
    if peer is None:
        return False
    if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
        _, uid, _ = struct.unpack(
            "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        return int(uid) == os.geteuid()
    if sys.platform == "darwin" and hasattr(peer, "dup"):
        with peer.dup() as actual:
            if not hasattr(actual, "getpeereid"):
                return False
            uid, _ = actual.getpeereid()
            return int(uid) == os.geteuid()
    return False


async def read_gateway_projection(path: Path, thread: str) -> dict[str, object]:
    """Get one bounded DTO; all transport/schema failures are unavailable.

    The UI caller MUST schedule this after first paint. No implicit gateway
    start, local SQLite substitute, retry action, monitor, or secret logging.
    """
    try:
        query = FieldCodec.decode(RecoveryRequest, {"thread": thread})
    except (TypeError, ValueError):
        return dict(_UNAVAILABLE)
    path = Path(path)
    if not _private_socket(path):
        return dict(_UNAVAILABLE)
    request = (json.dumps(FieldCodec.encode(query), separators=(",", ":")) + "\n").encode()
    if len(request) > 1024:
        return dict(_UNAVAILABLE)
    writer: asyncio.StreamWriter | None = None
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TIMEOUT

        def remaining() -> float:
            return max(0.001, deadline - loop.time())

        reader, connected = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=_MAX_REPLY + 1), remaining()
        )
        writer = connected
        if not _same_uid(connected):
            return dict(_UNAVAILABLE)
        connected.write(request)
        connected.write_eof()  # gateway requires EOF after exactly one bounded line
        await asyncio.wait_for(connected.drain(), remaining())
        raw = await asyncio.wait_for(reader.readline(), remaining())
        if not raw.endswith(b"\n") or len(raw) > _MAX_REPLY:
            return dict(_UNAVAILABLE)
        if await asyncio.wait_for(reader.read(1), remaining()):
            return dict(_UNAVAILABLE)
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique)
        if not _valid_projection(value, thread):
            return dict(_UNAVAILABLE)
        return cast("dict[str, object]", value)
    except (OSError, ValueError, UnicodeError, TimeoutError, RecursionError, struct.error):
        return dict(_UNAVAILABLE)
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 0.1)
