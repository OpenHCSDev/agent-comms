"""Strip #commanders from children that only copied it from their parent.

Before CommandersTag declared itself a role (inherited() is False), child_of and
commanded_child copied the parent's whole tag set, so every fork of a commander carried
#commanders and now counts as one. A child whose channel_scope_generation is still 0 holds
exactly the tags its registration copied from its parent: no tag edit ever touched it, so
the bus has no "joined #commanders" announcement from it. A child with a later tag edit declared its own
membership (alarm-shape joined at bus seq 41836) and keeps it. Commanded units record no
parent, so none can be shown to have copied the tag; the retained bus has none tagged.
The strip goes through Thread.preserve_registration_history, so the channel scope
generation advances like any tag edit. Delete this step after the live installation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from agent_comms.channel_targets import CommandersTag
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile, RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.registry_store import RegistryStore


class InheritedCommandersTag(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (RegistryFile,)

    def carry(self, *, apply: bool) -> Carried:
        registry = RegistryStore.at(self.root)
        self.require_confined(registry.path)
        if not registry.path.exists():
            return Carried(0)
        tag = CommandersTag.declared_name
        # child_of declares every child with no tags of its own and then copies its parent's,
        # so a child still at generation 0 holds only copied tags, whatever its parent holds now.
        copied = tuple(
            thread.name for thread in registry.snapshot().threads.values()
            if tag in thread.tags and thread.channel_scope_generation == 0 and thread.parent.names()
        )
        if apply and copied:
            MaintenanceBarrier(registry.path).read().require_held_by(
                MaintenanceHolder.current(), "Strip the inherited #commanders tag")
            with registry.editing() as edit:
                for name in copied:
                    thread = edit.document.require(name)
                    edit.document.threads[name] = replace(
                        thread, tags=thread.tags - {tag}).preserve_registration_history(thread)
                edit.commit()
        return Carried(
            len(copied),
            detail={"threads": len(copied)},
            rewritten=tuple(path.relative_to(self.root) for path in registry.written)
            if apply and copied else (),
        )
