from __future__ import annotations

import sqlite3
from collections.abc import Callable

import agent_comms.messaging.messages as messages

type RecordBuilder = Callable[[int, sqlite3.Connection], messages.BusRecord]
