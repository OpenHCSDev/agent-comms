"""Wire declaration for reading repository sources without editable checkout access.

Serving it is in agent_comms.transport.served_tools.repositories, which only the server imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_sources import RepositorySource
from agent_comms.transport.request import ToolRequest, tool_field
from agent_comms.transport.tool_result import ToolResult


@dataclass(frozen=True)
class RepositoryReadResult(ToolResult):
    repository: Repo


@dataclass(frozen=True, kw_only=True)
class CommsRepositoryReadTool(ToolRequest[RepositoryReadResult]):
    label = "Read repository"
    description = (
        "Acquire a read-only repository source without a goal or problem. "
        "Use an existing local path or github:OWNER/REPO; returned sources grant no edits."
    )
    repository: Annotated[RepositorySource, *tool_field("Existing path or github:OWNER/REPO")]
