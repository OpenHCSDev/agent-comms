"""Codex's stored rollout: one JSON record per line, in the Responses item format.

The app-server (codex_protocol) reports the same conversation in a different item format; both
decoders produce the same transcript updates.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from agent_comms.cli_bridges.codex_records import (
    ROLLOUT_RECORD,
    CodexRolloutProvenance,
    RolloutRecord,
    RolloutState,
)
from agent_comms.cli_bridges.history import CodexHistory, JsonLinesFile, OfflineHistory
from agent_comms.cli_bridges.runtime_updates import RuntimeUpdate
from agent_comms.nominal.field_codec import StoredSessionId


@dataclass(frozen=True)
class HistoricalInstruction(RuntimeUpdate):
    """Where one instruction of the source session is, so its exact text can be read later."""
    source: CodexRolloutProvenance

    def publish(self, output) -> None:
        pass

    def add_to(self, history) -> None:
        history.instructions.add(self.source)


class CodexRollout(JsonLinesFile[RolloutRecord], CodexHistory, OfflineHistory):
    """`~/.codex/sessions/.../rollout-*.jsonl`: Codex's stored session, which it appends to while it runs."""

    suffixes = frozenset({'.jsonl'})

    records = ROLLOUT_RECORD
    state = RolloutState

    def facts(self, stream: BinaryIO, boundary: int) -> Iterator[RuntimeUpdate]:
        """The first record names the session; the selected history may start long after it."""
        for offset, raw in self.complete_lines(stream, 0, boundary):
            record = self.decode_line(raw, offset)
            if not self.session.holds(StoredSessionId(record.session.id)):
                raise ValueError('The rollout belongs to a different Codex session.')
            yield from record.updates(self.start_state(), offset)
            return
        raise ValueError('A Codex rollout starts with its session metadata.')

    def record_updates(self, record: RolloutRecord, state: RolloutState, offset: int,
                       raw: bytes) -> Iterator[RuntimeUpdate]:
        yield from record.updates(state, offset)
        digest = hashlib.sha256(raw).hexdigest()
        for index, _ in enumerate(record.instructions()):
            yield HistoricalInstruction(CodexRolloutProvenance(
                str(self.source.resolve()), offset, len(raw), digest, index))
