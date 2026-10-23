"""Shared lifecycle algorithms; each domain supplies its own DeclaredFamily root.

Keep this ABC registry-free: DeclaredFamily roots own membership, and mixing it
into a root must never start a second registry for the same lifecycle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Self

from .lifecycle_capability import LifecycleCapability


class LifecycleState(LifecycleCapability, ABC):
    declared_name: ClassVar[str]

    @classmethod
    @abstractmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        """States reachable by an ordinary transition (including self, if allowed)."""

    @classmethod
    def may_become(cls, nxt: LifecycleState | type[LifecycleState]) -> bool:
        return False

    @classmethod
    def after_triage_pending_assignment(cls) -> bool:
        return False

    @classmethod
    def after_full_pending_assignment(cls) -> bool:
        return False

    @classmethod
    def after_engaged_assignment(cls) -> bool:
        return False

    @classmethod
    def after_deferred_assignment(cls) -> bool:
        return False

    @classmethod
    def after_prompt_starting_attempt(cls) -> bool:
        return False

    @classmethod
    def after_prompt_accepted_attempt(cls) -> bool:
        return False

    @classmethod
    def after_model_running_attempt(cls) -> bool:
        return False

    @classmethod
    def after_tool_running_attempt(cls) -> bool:
        return False

    @classmethod
    def after_compaction_attempt(cls) -> bool:
        return False

    @classmethod
    def after_settling_attempt(cls) -> bool:
        return False

    @classmethod
    def after_model_stalled_attempt(cls) -> bool:
        return False

    @classmethod
    def after_aborting_attempt(cls) -> bool:
        return False

    @classmethod
    def after_retrying_attempt(cls) -> bool:
        return False

    @classmethod
    def after_provider_unavailable_attempt(cls) -> bool:
        return False

    @classmethod
    def after_queued_execution(cls) -> bool:
        return False

    @classmethod
    def after_pending_execution(cls) -> bool:
        return False

    @classmethod
    def after_active_execution(cls) -> bool:
        return False

    @classmethod
    def after_deferred_execution(cls) -> bool:
        return False

    @classmethod
    def after_reserved_attempt(cls) -> bool:
        return False

    @classmethod
    def after_claimed_attempt(cls) -> bool:
        return False

    @classmethod
    def after_failed_attempt(cls) -> bool:
        return False

    @classmethod
    def after_ready_generation(cls) -> bool:
        return False

    @classmethod
    def after_reserved_generation(cls) -> bool:
        return False

    @classmethod
    def after_blocked_generation(cls) -> bool:
        return False

    @classmethod
    def after_open_goal(cls) -> bool:
        return False

    @classmethod
    def after_blocked_state(cls) -> bool:
        return False

    @classmethod
    def after_completed_goal(cls) -> bool:
        return False

    @classmethod
    def after_draining_phase(cls) -> bool:
        return False

    @classmethod
    def after_paused_phase(cls) -> bool:
        return False

    @classmethod
    def after_installing_phase(cls) -> bool:
        return False

    @classmethod
    def after_ready_phase(cls) -> bool:
        return False

    @classmethod
    def after_pending_response(cls) -> bool:
        return False

    @classmethod
    def after_publishing_response(cls) -> bool:
        return False

    @classmethod
    def after_deferred_response(cls) -> bool:
        return False

    @classmethod
    def after_thread_work_state(cls) -> bool:
        return False

    @classmethod
    @abstractmethod
    def members_with(cls, capability: type) -> tuple[type[Self], ...]:
        """Supplied by DeclaredFamily in each domain root."""

    @classmethod
    def transition_table(cls) -> dict[str, frozenset[str]]:
        return {
            member.declared_name: frozenset(
                state.declared_name
                for successor in member.successors()
                for state in cls.members_with(successor)
            )
            for member in cls.members_with(cls)
        }
