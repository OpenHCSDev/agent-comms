"""The shared entry boundary for asynchronous store work, independent of watch scheduling."""

from collections.abc import Callable
from pathlib import Path

from agent_comms.installer.native_build_inputs import GitLaunch
from agent_comms.storage.maintenance_barrier import FreshIngress


class WatchPhase(FreshIngress):
    """A phase enters only while maintenance and its Git acquisition lifetime admit it."""

    def enter_phase[Result](self, root: Path, operation: Callable[[], Result]) -> Result:
        GitLaunch.enter_phase()
        self.require_entry_open(root)
        return operation()
