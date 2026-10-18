"""Captured original/following input notifications; never permission to send again."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from .coordinator import Coordination

if TYPE_CHECKING:
    from .acp_extension import QueueScope
    from .input_drain import InputDrain
    from .input_disposition import InputDocument
    from .input_attempt import InputAttempt
    from .queued_input import QueuedInput
    from .turn_input_source import AcceptedFollowingInput, OriginalTurnInput


class StartedInput(ABC):
    @property
    @abstractmethod
    def started_keys(self) -> tuple[str, ...]: ...

    @property
    def read_keys(self) -> tuple[str, ...]:
        return self.started_keys

    @abstractmethod
    def outcome(self, document: InputDocument, claim_scope: int) -> InputAttempt: ...

    @abstractmethod
    def public_id(self, row: InputAttempt) -> str | None: ...

    @abstractmethod
    async def text(self, inputs: InputDrain, session_id: str) -> str | None: ...

    @abstractmethod
    def retire(self, inputs: InputDrain, session_id: str) -> None: ...

    @abstractmethod
    async def refused(self, inputs: InputDrain, session_id: str) -> None: ...

    async def started(self, inputs: InputDrain, session_id: str, scope: QueueScope) -> None:
        document = await Coordination.run_worker(partial(inputs.dispositions.read, self.read_keys))
        row = self.outcome(document, scope.claimed_generation)
        for key in self.started_keys:
            stored = document.rows.get(key)
            if stored is not None and not stored.unresolved:
                await inputs.emit_input_disposition(session_id, stored)
        await inputs.emit_input_started(
            session_id, await self.text(inputs, session_id), self.public_id(row),
            source_scope=scope, native_id=row.native_id,
        )
        self.retire(inputs, session_id)
        await inputs.emit_queue_state(session_id)


@dataclass(frozen=True)
class OriginalStartedInput(StartedInput):
    source: OriginalTurnInput

    @property
    def started_keys(self) -> tuple[str, ...]:
        return self.source.keys

    @property
    def read_keys(self) -> tuple[str, ...]:
        return (*self.source.keys, *self.source.notice_keys)

    def outcome(self, document: InputDocument, claim_scope: int) -> InputAttempt:
        # A grouped original must prove every captured member, not just its first.
        for key in self.started_keys:
            document.lookup(key).require_started(claim_scope)
        key = next(iter(self.source.notice_keys or self.started_keys), None)
        return document.lookup(key).require_started(claim_scope)

    def public_id(self, row: InputAttempt) -> str | None:
        return row.public_id if self.source.notice_keys else self.source.accepted_id

    def item(self, inputs: InputDrain, session_id: str) -> QueuedInput | None:
        accepted = self.source.accepted_id
        return inputs.queued_inputs.get(session_id, {}).get(accepted) if accepted is not None else None

    async def text(self, inputs: InputDrain, session_id: str) -> str | None:
        item = self.item(inputs, session_id)
        return await item.started(inputs) if item is not None else self.source.notice_text

    def retire(self, inputs: InputDrain, session_id: str) -> None:
        accepted = self.source.accepted_id
        if accepted is not None:
            inputs.queued_inputs.get(session_id, {}).pop(accepted, None)

    async def refused(self, inputs: InputDrain, session_id: str) -> None:
        # Only unbound reservations become NotSent. A native binding remains
        # UNKNOWN, and an already proved start remains started; neither replays.
        await Coordination.run_worker(partial(inputs.finish_original_inputs, self.source.keys))
        item = self.item(inputs, session_id)
        self.retire(inputs, session_id)
        if item is not None:
            await item.not_started(inputs)
        await inputs.emit_input_delivery_changed(session_id)
        await inputs.emit_queue_state(session_id)


@dataclass(frozen=True)
class FollowingStartedInput(StartedInput):
    input_id: str
    source: AcceptedFollowingInput | None
    item: QueuedInput | None

    @classmethod
    def capture(cls, inputs: InputDrain, session_id: str, input_id: str) -> FollowingStartedInput:
        return cls(
            input_id,
            inputs.following_sources.get(session_id, {}).get(input_id),
            inputs.queued_inputs.get(session_id, {}).get(input_id),
        )

    @property
    def started_keys(self) -> tuple[str, ...]:
        return self.source.keys if self.source is not None else ()

    def outcome(self, document: InputDocument, claim_scope: int) -> InputAttempt:
        key = self.started_keys[0] if len(self.started_keys) == 1 else None
        return document.lookup(key).require_started(claim_scope)

    def public_id(self, row: InputAttempt) -> str:
        return self.input_id

    async def text(self, inputs: InputDrain, session_id: str) -> str | None:
        return await self.item.started(inputs) if self.item is not None else None

    def retire(self, inputs: InputDrain, session_id: str) -> None:
        inputs.queued_inputs.get(session_id, {}).pop(self.input_id, None)

    async def refused(self, inputs: InputDrain, session_id: str) -> None:
        source = inputs.following_sources.get(session_id, {}).pop(self.input_id, None)
        if source is not None:
            await inputs.emit_input_delivery_changed(session_id)
        item = inputs.queued_inputs.get(session_id, {}).pop(self.input_id, None)
        if item is not None:
            await item.not_started(inputs)
            await inputs.emit_queue_state(session_id)
