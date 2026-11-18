"""One observer pass after a watched store changed, as ObservationService.run takes it."""

import asyncio

from agent_comms.ui_model.observation import ObservationService


async def watched_pass(service: ObservationService) -> None:
    """The watcher wakes the service (observe_again) and run takes one pass; a new revision
    is adopted only once the observer's own gap since its last settled pass has passed."""
    loop = asyncio.get_running_loop()
    await asyncio.sleep(max(0.0, service.settled_at + service.observe_gap - loop.time()))
    service.observe_again()
    await service.pass_once()
    # A pass can publish an answer before exhausting its stepping budget. Run
    # the same subsequent wakes as ObservationService.run before returning.
    while service.observe_due or service.adoption_owed:
        if not service.observe_due:
            await service.changed.wait()
        await service.pass_once()
