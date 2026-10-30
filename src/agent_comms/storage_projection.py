"""One call's acquired storage checks over the composition root, not a storage declaration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from .comms import Comms
from .registry_document import RegistrySnapshot
from .repo import Checkout, RepositoryReads
from .storage_rules import CheckoutAllocation, StorageRule, UnitStorage
from .threads import Thread

if TYPE_CHECKING:
    from .native_tool_call import CodingCall
    from .shell_effects import StorageEffect


@dataclass
class StorageProjection:
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
        return UnitStorage.read(self.rule, self.thread)

    def require_write(self, path: Path) -> None:
        self.rule.require_destination(self.thread, path)
        owners = self.snapshot.threads_for(self.thread.incarnation.require_recorded())
        if not owners:
            self.rule.deny('The source writer incarnation is no longer registered.')
        owner, = owners
        resolved = path.resolve()
        sources = Checkout.git_roots(resolved, *resolved.parents)
        store = self.comms.goals.todos
        allowed = frozenset(boundary for root in sources
                            for allocation in CheckoutAllocation.for_path(self.rule, owner, root)
                            for boundary in allocation.edit_boundary(self.rule, owner, root, store, self.reads))
        # A thread registered at a directory holding its scratch (unit-fieldplan at $HOME) still
        # owns that scratch: the rule gate's audit lives there, and refusing it deadlocked the unit.
        in_project = (resolved.is_relative_to(owner.project_path.resolve())
                      and not resolved.is_relative_to(self.rule.roots_of(owner).scratch.resolve()))
        if (sources or path.is_relative_to(self.rule.checkout_base) or in_project) and not any(
                resolved.is_relative_to(root) for root in allowed):
            self.rule.deny(f'{path} is a read/launch source, not allocated editable work of {owner.name}.')

    def check_call(self, call: CodingCall) -> tuple[StorageEffect, ...]:
        """Check the call's declared effects against this same lazy owner snapshot."""
        effects = call.storage_effects(self.thread.worktree.path)
        self.check(effects)
        return effects

    def check(self, effects: Sequence[StorageEffect]) -> None:
        """Preflight every effect of one call against this one snapshot."""
        for effect in effects:
            effect.check(self)
