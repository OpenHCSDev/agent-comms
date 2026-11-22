"""The background run tools: start a shell command detached, and read how runs stand.

The run family, its store and its watcher are agent_comms.transport.background_runs; every
tool's background=true shares them through ServedRequest.started_in_background.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.transport.background_runs import (
    DEFAULT_SECONDS,
    TAIL_BYTES,
    BackgroundRun,
    BackgroundRunStarted,
    BackgroundRunStore,
    ShellRun,
)
from agent_comms.transport.request import tool_field
from agent_comms.transport.tool_request import ThreadToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms_hooks import ToolRefusedError


@dataclass(frozen=True)
class BackgroundRunStatus:
    """One run as its record holds it, and the tail of its output."""

    run: BackgroundRun
    tail: str


@dataclass(frozen=True)
class BackgroundStatusRead(ToolResult):
    """One run, or every run this thread owns."""

    runs: tuple[BackgroundRunStatus, ...]


@dataclass(frozen=True, kw_only=True)
class CommsRunBackgroundTool(ThreadToolRequest, declared_name="run_background"):
    label = "Run in background"
    description = (
        "Start a shell command detached and return immediately: the turn keeps going while "
        "the command runs. Its output streams to a log file this tool owns; when the command "
        "exits (or its deadline passes and it is killed), a message with the outcome, its exit "
        "code and the log tail is sent to this thread. A bridge restart does not lose the run. "
        "Use background_status to poll or tail earlier."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    command: Annotated[str, *tool_field("Shell command to run detached")]
    seconds: Annotated[int, *tool_field("Kill the command after this many seconds")] = (
        DEFAULT_SECONDS
    )

    def apply(self, comms, caller) -> BackgroundRunStarted:
        if not self.command.strip():
            raise ToolRefusedError("A background command cannot be empty.")
        store = BackgroundRunStore.at(comms.root)
        run = ShellRun.launched(store.log_directory, self.thread, self.command, self.seconds)
        return store.start(run, run.wait, comms.messaging)


@dataclass(frozen=True, kw_only=True)
class CommsBackgroundStatusTool(ThreadToolRequest, declared_name="background_status"):
    label = "Background status"
    description = (
        "Report where one background run stands (running, exited with its code, killed at its "
        "deadline, returned, failed, or gone) and the tail of its log; without run_id, list "
        "every background run this thread owns, shell commands and background=true tool calls "
        "alike. This also picks up runs a bridge restart left behind."
    )
    thread: Annotated[str, *tool_field("Thread name")]
    run_id: Annotated[str, *tool_field("Run id from run_background; empty lists all")] = ""
    tail_bytes: Annotated[int, *tool_field("Bytes of log tail to return")] = TAIL_BYTES

    def apply(self, comms, caller) -> BackgroundStatusRead:
        store = BackgroundRunStore.at(comms.root)
        store.recover(comms.messaging)
        return BackgroundStatusRead(runs=tuple(
            BackgroundRunStatus(run, run.tail(self.tail_bytes))
            for run in store.for_thread(self.thread)
            if self.run_id in ("", run.run_id)
        ))
