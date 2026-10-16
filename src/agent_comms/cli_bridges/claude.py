"""Claude Code notifications and completed transcript blocks for Core."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from agent_comms.private_path import FileRevision
from .claude_protocol import CLAUDE_RECORD, ClaudeSession, ClaudeMonitorFrame
from .shared import (
    LineMonitorBridge, Options, TranscriptUpdate,
    parse_options, run_bridge, shared_parser,
)


class ClaudeBridge(LineMonitorBridge):
    incoming_frame = ClaudeMonitorFrame

    async def transcript(self) -> AsyncIterator[TranscriptUpdate]:
        """Follow this Claude file without submitting an input.

        On restart, items older than the last 4 MiB are not exported. Core's
        finish operation is idempotent, so completed items may safely overlap.
        Only the current status is published from that initial window.
        """
        matches = tuple((Path.home() / '.claude/projects').glob(
            f'*/{self.binding.thread}.jsonl'))
        if len(matches) != 1:
            raise ValueError(f'Expected one Claude transcript for {self.binding.thread}; found {len(matches)}')
        path, = matches
        session = ClaudeSession()
        last_status: TranscriptUpdate | None = None
        with path.open('rb') as stream:
            original = FileRevision.from_file(stream)
            start = max(0, original.size - 4 * 1024 * 1024)
            pending: bytes | None = b''
            if start:
                stream.seek(start - 1)
                if stream.read(1) != b'\n':
                    # None means this prefix began outside our captured window.
                    # Discard through its newline, even if it is still being written.
                    pending = None
            available = original.size - stream.tell()
            while True:
                chunk = stream.read(available)
                if pending is None:
                    boundary = chunk.find(b'\n')
                    if boundary >= 0:
                        pending = chunk[boundary + 1:]
                else:
                    pending += chunk
                if pending is not None:
                    lines = pending.split(b'\n')
                    pending = lines.pop()
                    for line in lines:
                        record = CLAUDE_RECORD.validate_json(line)
                        for update in record.updates(session):
                            if session.catching_up and update.is_status:
                                last_status = update
                            else:
                                yield update
                if session.catching_up:
                    session.catching_up = False
                    if last_status is not None:
                        yield last_status
                await asyncio.sleep(0.2)
                opened = FileRevision.from_file(stream)
                current = FileRevision.capture(path)
                if (current is None or current.identity != original.identity
                        or min(opened.size, current.size) < stream.tell()):
                    raise ValueError('Claude transcript was replaced or truncated')
                available = opened.size - stream.tell()


def main() -> None:
    parser = shared_parser(__doc__, Path.home() / '.local/state/claude-comms-inbox', ClaudeBridge)
    run_bridge(ClaudeBridge, parse_options(parser, Options))
