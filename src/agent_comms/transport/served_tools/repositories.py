"""Serving repository source reads on Core."""

from __future__ import annotations

import shlex
from pathlib import Path

from agent_comms.cli_support.checkout_grant import CheckoutGrant
from agent_comms.cli_verbs.tool import InvokeCliCommand
from agent_comms.comms import Comms
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.repository_sources import GitHubRepositorySource
from agent_comms.projects.repository_storage import RepositoryReader
from agent_comms.projects.shell_effects import ReadRepositoryStorageEffect
from agent_comms.projects.storage_projection import StorageProjection
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tool_request import HostArea, ThreadToolRequest
from agent_comms.transport.tools.repositories import (
    CommsRepositoryReadTool,
    RepositoryReadResult,
)


class RepositoryReadGrant(CheckoutGrant):
    """A registered caller can read a source without holding an editable allocation."""

    def require(self) -> None:
        self.thread.incarnation.require_recorded()


class ServedCommsRepositoryReadTool(
    HostArea, ThreadToolRequest, RepositoryReader, CommsRepositoryReadTool,
):
    """Serving CommsRepositoryReadTool."""

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
