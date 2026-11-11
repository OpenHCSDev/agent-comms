from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.projects.checkouts import Checkout, RepositoryReads
from agent_comms.projects.repository_sources import RepositorySource
from agent_comms.projects.repository_storage import RepositoryReader
from agent_comms.projects.shell_effects import StorageEffect
from agent_comms.projects.storage_admission import StorageAdmission
from agent_comms.projects.storage_rules import CheckoutAllocation, StorageRule, UnitStorage
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread
from agent_comms.transport.native_tool_call import CodingCall
from agent_comms.transport.tool_declaration import ToolDeclaration


@dataclass
class StorageProjection(StorageAdmission):
    """One call's lazy snapshot; ordinary writes never need a checkout inventory."""

    rule: StorageRule
    thread: Thread
    comms: Comms

    @cached_property
    def snapshot(self) -> RegistrySnapshot:
        return self.comms.registry.snapshot()

    @cached_property
    def reads(self) -> RepositoryReads:
        return RepositoryReads()

    @cached_property
    def storage(self) -> UnitStorage:
        return UnitStorage.read(self.rule, self.thread, self.reads)

    def require_write(self, path: Path) -> None:
        self.rule.require_destination(self.thread, path)
        owners = self.snapshot.threads_for(self.thread.incarnation.require_recorded())
        if not owners:
            self.rule.deny("The source writer incarnation is no longer registered.")
        (owner,) = owners
        resolved = path.resolve()
        sources = Checkout.git_roots(resolved, *resolved.parents)
        scratch = self.rule.roots_of(owner).scratch.resolve()
        # The registry's scratch allocation is more specific than an enclosing
        # source tree. A repository rooted in that allocation is still source.
        if resolved.is_relative_to(scratch) and not any(
                root.is_relative_to(scratch) for root in sources):
            return
        store = self.comms.goals.todos
        allowed = frozenset(
            boundary
            for root in sources
            for allocation in CheckoutAllocation.for_path(self.rule, owner, root)
            for boundary in allocation.edit_boundary(self.rule, owner, root, store, self.reads)
        )
        # A checkout this conversation moved away from stays editable: leaving a
        # project does not strip the thread's ownership of the work it did there.
        # The current project is unchanged: being home still grants no edit.
        allowed = allowed.union(project.resolved() for project in owner.earlier_projects)
        in_project = resolved.is_relative_to(owner.project_path.resolve())
        if (sources or path.is_relative_to(self.rule.checkout_base) or in_project) and not any(
            resolved.is_relative_to(root) for root in allowed
        ):
            self.rule.deny(
                f"{path} is a read/launch source, not allocated editable work of {owner.name}: "
                "editing it needs an open problem for that repository, so run "
                f"`agent-comms-team request add --as {owner.name} \"...\"`, then "
                "`agent-comms-team order problem ORDER.json`, then "
                f"`agent-comms repository-checkout --from {owner.name} add P<n> OWNER/REPO`."
            )

    def deny(self, reason: str) -> None:
        self.rule.deny(reason)

    def deny_repository_operation(self, reason: str) -> None:
        examples = tuple(
            reader.example(self.comms.root)
            for reader in ToolDeclaration.members_with(RepositoryReader)
        )
        self.deny(f"{reason}. Read without an editable allocation: " + '; '.join(examples))

    def require_floor(self) -> None:
        self.rule.require_floor()

    def require_checkout(self, path: Path, repository: RepositorySource) -> None:
        rule, thread = self.rule, self.thread
        rule.require_destination(thread, path)
        rule.require_physical_allocation(path)
        if not rule.owns_checkout(thread, path, repository.github, self.reads):
            destination = rule.order_checkout(rule.roots_of(thread), "P<number>")
            rule.deny(f'checkout destination must be {destination}')
        selected = rule.reused_checkout(thread, repository.github, path, self.reads)
        if selected != path:
            rule.deny(f'Reuse {selected}; this agent already has a checkout of '
                      f'{repository.github}. Do not create another for a new goal/order.')
        self.storage = self.storage.adding(Checkout(path))

    def require_repository_donors(self, repository: RepositorySource) -> None:
        for donor in repository.allocations(self.rule, self.thread):
            self.require_floor()
            self.rule.require_repository_donor(self.thread, repository.github, donor)

    def remove_checkout(self, path: Path) -> None:
        if path == self.thread.project_path:
            self.rule.deny(f'{path} is where {self.thread.name} runs; choose another existing project first: agent-comms set-project {self.thread.name} <existing-project>')
        if any(repository.checkout.resolve() == path
               for repository in self.snapshot.team_project.unique_repositories()):
            self.rule.deny(f'{path} is a registered repository, not a disposable checkout')
        self.rule.require_isolated_checkout(
            self.thread, path, self.snapshot.threads.values(), self.reads)
        self.storage = self.storage.removing(Checkout(path))

    def check_call(self, call: CodingCall) -> tuple[StorageEffect, ...]:
        """Check the call's declared effects against this same lazy owner snapshot."""
        effects = call.storage_effects(self.thread.worktree.path)
        self.check(effects)
        return effects

    def check(self, effects: Sequence[StorageEffect]) -> None:
        """Preflight every effect of one call against this one snapshot."""
        for effect in effects:
            effect.check(self)
