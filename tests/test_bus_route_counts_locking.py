"""Route-count writers decode outside locks; readers use committed WAL snapshots."""

from agent_comms.read_basis import NamedViewer
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest

from agent_comms.bus_route_counts import BusRouteCounts, CompletingRouteCounts, RouteIndexRebuilding
from agent_comms.comms import wire
from agent_comms.routing import DeliveryMessage
from agent_comms.threads import Thread
from agent_comms.wire_log import CurrentSnapshotOpening


def _open_source(comms):
    return comms.bus.log.opened_wire_snapshot(opening=CurrentSnapshotOpening())


def _sync(comms, decode=None):
    with _open_source(comms) as source:
        return BusRouteCounts(comms.bus.log.path).sync(
            source,
            decode or (lambda row: DeliveryMessage.from_wire(
                row, root_id=source.metadata.root_id
            )),
        )


