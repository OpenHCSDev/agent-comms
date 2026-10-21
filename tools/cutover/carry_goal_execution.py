"""Remove backend-produced registry blocks once; explicit decisions stay stored.

The retired texts belong only to this cutover. Delete this step after rollout;
no turn, retry, timer or installation hook uses them to authorize execution.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_comms.goal_states import ActiveGoal, BlockedGoal
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import RegistryFile
from agent_comms.registry_store import RegistryStore

FAILED_BACKEND_REASON = "Backend turn failed; inspect local diagnostics before resuming."
BACKEND_REASONS = frozenset(
    {
        FAILED_BACKEND_REASON,
        (
            "Backend reported success without assistant output or tool activity; inspect "
            "the session before resuming."
        ),
        "Goal turn ended without verified terminal progress.",
        "Backend turn ended without a result; inspect local diagnostics.",
        "Goal origin turn did not finish successfully.",
        (
            "Standby wake launch authority unavailable; inspect the UNKNOWN input before "
            "explicit Retry. No input was replayed."
        ),
        (
            "The interrupted goal attempt is unresolved. Inspect it, then use Retry to "
            "authorize a new attempt. Your messages can still be sent."
        ),
        "Goal attempt unresolved; inspect diagnostics before Retry.",
    }
)


class BackendGoalBlocks(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        store = RegistryStore(RegistryFile.path_in(root))
        paths = tuple(path for path in store.written if path.exists())
        for path in paths:
            cls.require_confined(root, path)
        return paths

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        store = RegistryStore(RegistryFile.path_in(root))
        cls.require_confined(root, store.path)
        if not store.path.exists():
            return Carried(0)
        migrated = preserved = 0
        with store.editing() as edit:
            for owner in tuple(edit.document.threads.values()):
                for goal in owner.goal:
                    if isinstance(goal.state, BlockedGoal):
                        if goal.state.block_reason in BACKEND_REASONS:
                            migrated += 1
                            active = replace(goal, state=ActiveGoal(), revision=goal.revision + 1)
                            edit.document.install_thread(replace(owner, goal=active))
                        else:
                            preserved += 1
            if apply and migrated:
                edit.commit()
        rewritten = (
            tuple(str(path.relative_to(root)) for path in store.written)
            if apply and migrated
            else ()
        )
        return Carried(
            migrated, detail={"migrated": migrated, "preserved": preserved}, rewritten=rewritten
        )
