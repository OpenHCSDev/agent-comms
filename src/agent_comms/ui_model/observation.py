"""Core observation for the comms UI, in a process of its own.

A UI process asks what to observe (the sidebar, the threads its views show)
and receives finished results only when Core's stores actually change: the
service watches the store files that ``HistoryViews.revision_paths`` declares,
reads snapshots and presentations, derives the sidebar rows, and sends them
over a pipe. The UI also asks it for reads it awaits: store reads (transcript
pages, their currency, message notifications), answered in arrival order, and
reads a thread's live owner answers over its socket (the goal snapshot, input
delivery), answered as they complete. Each answer carries the request id and
the decoded value or the exception the read raised. Store work runs in the
service process. The receiving process reads and unpickles each complete packet
in a joined worker; only typed delivery and future settlement run on its loop.
The receiving worker still shares its interpreter lock with the UI.
"""

from __future__ import annotations
from agent_comms.repo import Checkout

import asyncio
import itertools
import multiprocessing
import signal
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack, aclosing
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.acp_extension import (
    TranscriptDraftUpdate,
    TranscriptSnapshotUpdate,
)
from agent_comms.active_route import CommsRoute
from agent_comms.activity import ObservedActivity
from agent_comms.bus_read import OperatorReader, OwnerLiveRead, OwnerReadRuntimeRequest, UiRead
from agent_comms.child_process import join_retirement
from agent_comms.commander_hud import CommanderFrame, CommanderHud
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.owner_connection import OwnerConnection, OwnerReading, OwnerReplyConnection
from agent_comms.coordination_errors import (
    CoordinationError,
    CoordinationReadUnavailable,
    DeferredRead,
    ReloadRequired,
    Refused,
    StaleRevision,
)
from agent_comms.errors import RelationViolationError, ToolRefusedError
from agent_comms.history_views import RetiredViews
