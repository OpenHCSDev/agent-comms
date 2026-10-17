"""Serial preparation of the existing route index on the private prefix."""
from pathlib import Path
from agent_comms.bus_route_counts import BusRouteCounts
from agent_comms.routing import DeliveryMessage
from agent_comms.wire_log import WireLog
s=Path('/var/tmp/agent-scratch-p156'); log=WireLog(s/'bus.jsonl'); index=BusRouteCounts(log.path)
with log._opened_wire_snapshot() as opened:
    decode=lambda record: tuple(DeliveryMessage.from_wire(record,root_id=opened.metadata.root_id))
    state=index.sync(opened.stream,opened.revision,decode)
    while not state.complete:
        state=index.sync(opened.stream,opened.revision,decode)
print('normal private route index ready',flush=True)
