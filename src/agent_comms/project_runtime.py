"""Original project observation through one retained controller binding."""
from __future__ import annotations
from dataclasses import dataclass
from .coordinator import Coordination
from .runtime_request import ResultRuntimeRequest
from .thread_presentation import LiveThreadOwnerBinding

@dataclass(frozen=True, kw_only=True)
class ProjectRuntimeRequest(ResultRuntimeRequest):
    """Current project observation through the original native launch binding.

    This command grants no input or tool execution. A retained native process
    must still observe its original registry lease before each project check.
    """
    binding: LiveThreadOwnerBinding

    @classmethod
    def for_native(cls, snapshot, thread):
        process = thread.require_process()
        owner = snapshot.owner_identity(thread.name)
        snapshot.require_owner_process(owner, process)
        return cls(thread=thread.name, binding=LiveThreadOwnerBinding(owner, process))

    def require_original(self, snapshot):
        if not self.binding.owner.incarnation.matches_recorded_name(self.thread, snapshot):
            raise ValueError("Project request names another original thread")
        snapshot.require_owner_process(self.binding.owner, self.binding.process)
        return snapshot.require_active(self.binding.owner.incarnation.name)

    def require_owner(self, snapshot):
        self.require_original(snapshot)
        return super().require_owner(snapshot)

    async def result(self, ctx):
        snapshot = await Coordination.run_worker(ctx.server.agent._comms.registry.snapshot)
        current = self.require_original(snapshot)
        return {"worktree": current.worktree}
