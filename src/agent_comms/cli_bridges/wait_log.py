"""Lightweight event-driven watcher for persistent inbox-watch log files.

Uses `ChangedSources` (`agent_comms.storage.native_changes`) directly without importing Core's
`Bus`, `aiohttp`, or full `inbox_watch_client` stack, cutting `--wait-log` startup from ~3.2s
and ~123 MB RSS down to ~0.2s and ~32 MB RSS.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agent_comms.storage.native_changes import ChangedSources, FileSource

_PTY_CRLF_RE = re.compile(r"\r+\n")


def _log_cursor_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.cursor")


def _read_log_cursor(cursor_path: Path) -> tuple[int, int] | None:
    try:
        raw = json.loads(cursor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    inode = raw.get("inode")
    offset = raw.get("offset")
    if isinstance(inode, int) and isinstance(offset, int) and offset >= 0:
        return (inode, offset)
    return None


def _write_log_cursor(cursor_path: Path, inode: int, offset: int) -> None:
    tmp = cursor_path.with_name(f".{cursor_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"inode": inode, "offset": offset}), encoding="utf-8")
    os.replace(tmp, cursor_path)


@dataclass
class LogGrowthState:
    """Tracks one watch log's `(inode, offset)` cursor and reads newly appended text."""

    target: Path
    offset: int | None = None
    cur_inode: int | None = None
    pos: int = 0

    @property
    def cursor_path(self) -> Path:
        return _log_cursor_path(self.target)

    def poll(self) -> str | None:
        if not self.target.exists():
            return None
        stat = self.target.stat()
        if self.cur_inode is None:
            self.cur_inode = stat.st_ino
            if self.offset is not None:
                self.pos = self.offset
            else:
                saved = _read_log_cursor(self.cursor_path)
                if saved is None:
                    self.pos = stat.st_size
                    _write_log_cursor(self.cursor_path, self.cur_inode, self.pos)
                elif saved[0] != self.cur_inode or stat.st_size < saved[1]:
                    self.pos = 0
                else:
                    self.pos = saved[1]
        cur = stat.st_size
        if stat.st_ino != self.cur_inode or cur < self.pos:
            self.cur_inode = stat.st_ino
            self.pos = 0
        if cur <= self.pos:
            return None
        with self.target.open("rb") as fh:
            fh.seek(self.pos)
            raw = fh.read()
        new_pos = self.pos + len(raw)
        self.pos = new_pos
        if self.offset is None:
            _write_log_cursor(self.cursor_path, self.cur_inode, new_pos)
        text = raw.decode("utf-8", errors="replace")
        return _PTY_CRLF_RE.sub("\n", text).replace("\r", "\n")


def wait_for_log_growth_sync(path: Path, *, offset: int | None = None) -> str:
    """Block on native inotify until `path` grows, then return normalized appended text."""
    target = path.expanduser()
    state = LogGrowthState(target, offset=offset)
    with ChangedSources(FileSource(target)) as changes:
        while True:
            changes.changes()
            if (chunk := state.poll()) is not None:
                return chunk
            changes.block_until_change(60.0)


async def wait_for_log_growth(path: Path, *, offset: int | None = None) -> str:
    """Async event-driven wrapper over `ChangedSources` for waiting on a watch log."""
    target = path.expanduser()
    state = LogGrowthState(target, offset=offset)
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    with ChangedSources(FileSource(target)) as changes:
        loop.add_reader(changes.fd, ready.set)
        try:
            while True:
                ready.clear()
                changes.changes()
                if (chunk := state.poll()) is not None:
                    return chunk
                await ready.wait()
        finally:
            loop.remove_reader(changes.fd)
