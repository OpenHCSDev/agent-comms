from __future__ import annotations

import sqlite3
from collections.abc import Callable


type RecordBuilder = Callable[[int, sqlite3.Connection], messages.BusRecord]

from agent_comms.messaging import messages
