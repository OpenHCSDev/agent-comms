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
import stat
import struct
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

_MAX_REPLY = 4096
_TIMEOUT = 0.75
_UNAVAILABLE: dict[str, object] = {
    "schema": 1,
    "availability": "unavailable",
    "reason": "gateway_unavailable",
}
_REASONS = frozenset(
    {
        "gateway_unavailable",
        "missing",
        "invalid_store",
        "unsupported_schema",
        "busy",
        "unknown_owner",
    }
)
_STATUSES = frozenset({"queued", "pending", "active", "deferred", "completed", "failed"})
_ORIGINS = frozenset({"wire", "acp", "goal", "system"})
_PHASES = frozenset(
    {
        "prompt_starting",
        "prompt_accepted",
        "model_running",
        "tool_running",
        "compaction",
        "settling",
        "model_stalled",
        "aborting",
        "retrying",
        "provider_unavailable",
        "succeeded",
        "attempt_failed",
    }
)
_PUBLICATIONS = frozenset({"pending", "uncertain", "deferred", "published", "silent", "failed"})
_RECOVERIES = frozenset(
    {
        "model_stalled",
        "aborting",
        "retrying",
        "provider_unavailable",
        "deferred",
        "failed",
        "recovered",
    }
)


def _exact(value: object, fields: set[str]) -> bool:
    return type(value) is dict and set(value) == fields


def _nonnegative(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _valid_projection(value: object, thread: str) -> bool:
    if not isinstance(value, dict) or value.get("schema") != 1 or type(value["schema"]) is not int:
        return False
    if value.get("availability") == "unavailable":
        return (
            _exact(value, {"schema", "availability", "reason"})
            and type(value["reason"]) is str
            and value["reason"] in _REASONS
        )
    if not _exact(
        value,
        {
            "schema",
            "availability",
            "owner",
            "sampledAtMs",
            "current",
            "lastRecovery",
            "connectivity",
        },
    ):
        return False
    if (
        value["availability"] != "available"
        or value["owner"] != thread
        or not _nonnegative(value["sampledAtMs"])
    ):
        return False
    current, recovery, connection = value["current"], value["lastRecovery"], value["connectivity"]
    if current is not None:
        if not _exact(
            current, {"status", "origin", "isCurrent", "attempt", "canRetry", "publication"}
        ):
            return False
        if (
            type(current["status"]) is not str
            or current["status"] not in _STATUSES
            or type(current["origin"]) is not str
            or current["origin"] not in _ORIGINS
            or type(current["isCurrent"]) is not bool
            or type(current["canRetry"]) is not bool
            or (
                current["publication"] is not None
                and (
                    type(current["publication"]) is not str
                    or current["publication"] not in _PUBLICATIONS
                )
            )
        ):
            return False
        attempt = current["attempt"]
        if attempt is not None and (
            not _exact(attempt, {"ordinal", "phase", "backendDone", "backendProcessExited"})
            or not _nonnegative(attempt["ordinal"], 1)
            or type(attempt["phase"]) is not str
            or attempt["phase"] not in _PHASES
            or type(attempt["backendDone"]) is not bool
            or type(attempt["backendProcessExited"]) is not bool
        ):
            return False
    if recovery is not None and (
        not _exact(recovery, {"kind", "attempt", "elapsedMs", "observedAtMs"})
        or type(recovery["kind"]) is not str
        or recovery["kind"] not in _RECOVERIES
        or not _nonnegative(recovery["attempt"], 1)
        or not _nonnegative(recovery["elapsedMs"])
        or not _nonnegative(recovery["observedAtMs"])
    ):
        return False
    return connection is None or not (
        not _exact(connection, {"owner", "acpClient", "observedAtMs"})
        or type(connection["owner"]) is not str
        or connection["owner"] not in {"connected", "reconnecting", "offline"}
        or type(connection["acpClient"]) is not str
        or connection["acpClient"] not in {"connected", "disconnected"}
        or not _nonnegative(connection["observedAtMs"])
    )


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
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
            ):
                return False
        if root.lstat().st_uid != os.geteuid() or stat.S_IMODE(root.lstat().st_mode) != 0o700:
            return False
        directory = path.parent.lstat()
        endpoint = path.lstat()
        return (
            stat.S_ISDIR(directory.st_mode)
            and directory.st_uid == os.geteuid()
            and stat.S_IMODE(directory.st_mode) == 0o700
            and stat.S_ISSOCK(endpoint.st_mode)
            and endpoint.st_uid == os.geteuid()
            and stat.S_IMODE(endpoint.st_mode) == 0o600
        )
    except OSError:
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
    start, local SQLite fallback, retry action, monitor, or secret logging.
    """
    if (
        type(thread) is not str
        or not 1 <= len(thread) <= 256
        or any(ord(character) < 32 or ord(character) == 127 for character in thread)
    ):
        return dict(_UNAVAILABLE)
    path = Path(path)
    if not _private_socket(path):
        return dict(_UNAVAILABLE)
    request = (json.dumps({"thread": thread}, separators=(",", ":")) + "\n").encode()
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
