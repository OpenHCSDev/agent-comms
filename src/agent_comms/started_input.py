"""Shared start notification behavior on the existing turn input sources."""
from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar

from agent_comms.lifecycle_capability import Unresolved

from .coordinator import Coordination

if TYPE_CHECKING:
    from .acp_extension import InputStartedUpdate, QueueScope
    from .input_attempt import StartedInput as StartedRow
    from .input_drain import InputDrain
    from .turn_input_source import TurnInputSource


class StartedInput:
    notice_text: ClassVar[str | None] = None

    def started_update_of(self, row: StartedRow, update: InputStartedUpdate) -> InputStartedUpdate:
        return update

    def failure_state(self, document):
        return None

    def failed_input_text(self, document) -> str | None:
        return None

    async def started(self: TurnInputSource, inputs: InputDrain, session_id: str, scope: QueueScope) -> None:
        document = await Coordination.run_worker(partial(
            inputs.dispositions.read, self.keys,
        ))
        key = next(iter(self.keys), None)
        row = document.lookup(key).require_started(scope.claimed_generation)
        if any(stored is not None and not stored.items_with(Unresolved, stored)
               for stored in map(document.rows.get, self.keys)):
            await inputs.emit_input_delivery_changed(session_id)
        await inputs.runtime.publish_update(
            session_id=session_id, update=(await self.acceptance.started_update(
                self, inputs, session_id, row, scope,
            )).acp_chunk(),
        )
        self.acceptance.release(inputs, session_id)
        await inputs.emit_queue_state(session_id)
