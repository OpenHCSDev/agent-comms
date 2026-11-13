"""Joined blocking work without opening or importing coordination stores."""

from collections.abc import Callable
from contextvars import Context
from functools import partial

from agent_comms.execution.child_process import AsyncWork
from agent_comms.execution.publication_measurements import WorkCpu
from agent_comms.installer.native_build_inputs import GitLaunch
from agent_comms.storage.maintenance_barrier import MaintenanceHolder
from agent_comms.storage.store_files import StoreLock


class CoordinationWork:
    """The shared resource context for blocking store and registry operations."""

    @staticmethod
    def worker_context() -> Context:
        return GitLaunch.in_context(MaintenanceHolder.in_context(StoreLock.worker_context()))

    @staticmethod
    async def run_worker[Result](operation: Callable[[], Result]) -> Result:
        """Wait for blocking work before its caller releases the enclosing resources.

        The callback owns and closes every borrowed resource it opens. This
        scheduling seam also supports registry-only acquisition/retirement;
        those operations must not depend on opening a coordinator connection.
        """
        return await AsyncWork.run_async(
            partial(CoordinationWork.worker_context().run, WorkCpu.measured(operation)))
