"""Read repository sources without granting editable checkout access."""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_verbs.cli_commands import CheckoutGrant
from agent_comms.cli_verbs.tool import InvokeCliCommand
from agent_comms.comms import Comms
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.repository_sources import GitHubRepositorySource, RepositorySource
from agent_comms.projects.repository_storage import RepositoryReader
from agent_comms.projects.shell_effects import ReadRepositoryStorageEffect
from agent_comms.projects.storage_projection import StorageProjection
from agent_comms.transport.request import tool_field
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_request import ThreadToolRequest
from agent_comms.transport.tool_result import ToolResult


class RepositoryReadGrant(CheckoutGrant):
    """A registered caller can read a source without holding an editable allocation."""

    def require(self) -> None:
        self.thread.incarnation.require_recorded()


@dataclass(frozen=True)
class RepositoryReadResult(ToolResult):
    repository: Repo


@dataclass(frozen=True, kw_only=True)
class CommsRepositoryReadTool(ThreadToolRequest, RepositoryReader):
    label = "Read repository"
    description = (
        "Acquire a read-only repository source without a goal or problem. "
        "Use an existing local path or github:OWNER/REPO; returned sources grant no edits."
    )
    repository: Annotated[RepositorySource, *tool_field("Existing path or github:OWNER/REPO")]

    @classmethod
    def example(cls, root: Path) -> str:
        request = cls(repository=GitHubRepositorySource(RepositoryName("OWNER", "REPO")))
        command = InvokeCliCommand(
            caller=ThreadCaller("UNIT"), tool=cls.declared_name, arguments=request.arguments,
        )
        return shlex.join(command.argv(root))

    def apply(self, comms: Comms, caller: ThreadCaller) -> RepositoryReadResult:
        grant = RepositoryReadGrant(caller.authority(comms))
        grant.revalidate(comms)
        rule = comms.registry.storage_rule
        projection = StorageProjection(rule, grant.thread, comms)
        ReadRepositoryStorageEffect(self.repository).check(projection)
        repository = self.repository.readable(rule, grant.thread, projection.reads)
        grant.revalidate(comms)
        return RepositoryReadResult(repository)
