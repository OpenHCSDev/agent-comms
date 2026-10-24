"""Shared start notification behavior on the existing turn input sources."""
from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar

from agent_comms.lifecycle_capability import Unresolved

from .coordinator import Coordination

if TYPE_CHECKING:
    from .acp_extension import QueueScope
    from .input_drain import InputDrain
    from .turn_input_source import TurnInputSource


class StartedInput:
    notice_keys: ClassVar[tuple[str, ...]] = ()
    notice_text: ClassVar[str | None] = None

    async def started(self: TurnInputSource, inputs: InputDrain, session_id: str, scope: QueueScope) -> None:
        document = await Coordination.run_worker(partial(
            inputs.dispositions.read, (*self.keys, *self.notice_keys),
        ))
        key = next(iter(self.notice_keys or self.keys), None)
        row = document.lookup(key).require_started(scope.claimed_generation)
        for key in self.keys:
            stored = document.rows.get(key)
            if stored is not None and not stored.items_with(Unresolved, stored):
                await inputs.emit_input_disposition(session_id, stored)
        await inputs.runtime.publish_update(
            session_id=session_id, update=(await self.acceptance.started_update(
                self, inputs, session_id, row, scope,
            )).acp_chunk(),
        )
        self.acceptance.release(inputs, session_id)
        await inputs.emit_queue_state(session_id)
