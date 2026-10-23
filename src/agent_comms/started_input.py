"""Shared start notification behavior on the existing turn input sources."""
from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar

from agent_comms.lifecycle_capability import Unresolved

from .acp_extension import InputStartedUpdate, NoQueueScope
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
        item = inputs.state(session_id).queued.get(self.accepted_id)
        text = await item.started(inputs) if item is not None else self.notice_text
        public_id = row.public_id if self.notice_keys else self.accepted_id
        current = await inputs.queue_binding(session_id)
        if not scope.current_for(current):
            current = NoQueueScope()
        await inputs.runtime.publish_update(
            session_id=session_id, update=InputStartedUpdate(
                public_id, text, current, current.start_revision(inputs, session_id), row.native_id,
            ).acp_chunk(),
        )
        inputs.state(session_id).queued.pop(self.accepted_id, None)
        await inputs.emit_queue_state(session_id)
