"""Shared lifecycle behavior composed by the domain state declarations."""

from __future__ import annotations


class LifecycleCapability:
    """The neutral behavior shared by lifecycle families, without another registry."""

    @classmethod
    def require_nonatomic_advance(cls) -> None:
        """An unfinished state may be recorded before atomic settlement."""

    @classmethod
    def success_count(cls) -> int:
        """Contribution to an aggregate of successful results."""
        return 0

    @classmethod
    def reject_success_as_failure(cls, refusal: Exception) -> None:
        """An observation without success may supply failure evidence."""

    @classmethod
    def require_successful_completion(cls, refusal: Exception) -> None:
        raise refusal

    def active_items[T](self, *items: T) -> tuple[T, ...]:
        return ()

    def pending_items[T](self, *items: T) -> tuple[T, ...]:
        return ()

    def executing_items[T](self, *items: T) -> tuple[T, ...]:
        return ()

    def unresolved_items[T](self, *items: T) -> tuple[T, ...]:
        return ()

    def completed_items[T](self, *items: T) -> tuple[T, ...]:
        return ()

    def unsettled_items[T](self, *items: T) -> tuple[T, ...]:
        return items

    @classmethod
    def require_no_pending(cls, refusal: Exception) -> None:
        pass

    @classmethod
    def require_review(cls) -> None:
        raise ValueError("Reviewed inputs changed; inspect them again.")

    def accepts_interrupted_execution(self) -> bool:
        return False

    def resume_response(self, session, obligation, execution_id: str) -> None:
        """A response that was not deferred has nothing to reengage."""


class Settled(LifecycleCapability):
    def unsettled_items[T](self, *items: T) -> tuple[T, ...]:
        return ()


class Terminal(Settled):
    """A settled state has no ordinary successor and cannot advance piecemeal."""

    @classmethod
    def successors(cls) -> tuple[type, ...]:
        return ()

    @classmethod
    def require_nonatomic_advance(cls) -> None:
        from .coordination_errors import IdentityConflict

        raise IdentityConflict("terminal phases require atomic settlement")


class Failed(LifecycleCapability):
    """Failure evidence can support an interrupted execution, including a retry."""

    def accepts_interrupted_execution(self) -> bool:
        return True


class Pending(LifecycleCapability):
    def pending_items[T](self, *items: T) -> tuple[T, ...]:
        return items

    @classmethod
    def require_no_pending(cls, refusal: Exception) -> None:
        raise refusal


class Active(LifecycleCapability):
    def active_items[T](self, *items: T) -> tuple[T, ...]:
        return items

    def require_active(self) -> None:
        """Active members need no state-specific refusal."""


class Executing(LifecycleCapability):
    def executing_items[T](self, *items: T) -> tuple[T, ...]:
        return items

    def require_executing(self) -> None:
        """An observed execution satisfies its owner's start requirement."""


class Unresolved(LifecycleCapability):
    def unresolved_items[T](self, *items: T) -> tuple[T, ...]:
        return items

    @classmethod
    def require_review(cls) -> None:
        pass


class Deferred(LifecycleCapability):
    def resume_response(self, session, obligation, execution_id: str) -> None:
        from .coordination_tables.responses import ResponseObligation
        from .obligation_states import PendingResponse
        from .reason_codes import NoReasonCode

        ResponseObligation.update(
            session._connection,
            where="execution_id=? AND exact_target=?",
            parameters=(execution_id, obligation.exact_target),
            lifecycle=PendingResponse(), revision=obligation.revision + 1,
            reason_code=NoReasonCode(), updated_at_ms=session.now(obligation.updated_at_ms),
        )


class Complete(Settled):
    def completed_items[T](self, *items: T) -> tuple[T, ...]:
        return items

    def assert_complete(self) -> None:
        """The outcome has been recorded; no uncertain work remains."""

    def accepts_current_request(self, call, candidate) -> bool:
        return False


class Successful(Terminal):
    @classmethod
    def success_count(cls) -> int:
        return 1

    @classmethod
    def reject_success_as_failure(cls, refusal: Exception) -> None:
        raise refusal

    @classmethod
    def require_successful_completion(cls, refusal: Exception) -> None:
        pass
