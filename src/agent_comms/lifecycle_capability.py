"""Shared lifecycle behavior composed by the domain state declarations."""

from __future__ import annotations

class LifecycleCapability:
    """The neutral behavior shared by lifecycle families, without another registry."""

    @classmethod
    def require_nonatomic_advance(cls, refusal: type[Exception]) -> None:
        """An unfinished state may be recorded before atomic settlement."""

    @classmethod
    def success_count(cls) -> int:
        """Contribution to an aggregate of successful results."""
        return 0

    @classmethod
    def reject_success_as_failure(cls, refusal: Exception) -> None:
        if cls.success_count():
            raise refusal

    @classmethod
    def require_successful_completion(cls, refusal: Exception) -> None:
        if not cls.success_count():
            raise refusal

    @classmethod
    def items_with[T](cls, capability: type[LifecycleCapability], *items: T) -> tuple[T, ...]:
        """Project values using declared nominal membership, without another flag store."""
        return items if issubclass(cls, capability) else ()

    @classmethod
    def require_no_pending(cls, refusal: Exception) -> None:
        pass

    @classmethod
    def require_review(cls) -> None:
        raise ValueError("Reviewed inputs changed; inspect them again.")

    def require_failure(self, refusal: Exception) -> None:
        raise refusal

    def accepts_interrupted_execution(self) -> bool:
        return False


class Settled(LifecycleCapability):
    pass


class Terminal(Settled):
    """A settled state has no ordinary successor and cannot advance piecemeal."""

    @classmethod
    def successors(cls) -> tuple[type[LifecycleCapability], ...]:
        return ()

    @classmethod
    def require_nonatomic_advance(cls, refusal: type[Exception]) -> None:
        raise refusal("terminal phases require atomic settlement")


class Failed(LifecycleCapability):
    """Failure evidence can support an interrupted execution, including a retry."""

    def require_failure(self, refusal: Exception) -> None:
        pass

    def accepts_interrupted_execution(self) -> bool:
        return True


class Pending(LifecycleCapability):
    @classmethod
    def require_no_pending(cls, refusal: Exception) -> None:
        raise refusal


class Active(LifecycleCapability):
    def require_active(self) -> None:
        """Active members need no state-specific refusal."""


class Executing(LifecycleCapability):
    def require_executing(self) -> None:
        """An observed execution satisfies its owner's start requirement."""


class Unresolved(LifecycleCapability):
    @classmethod
    def require_review(cls) -> None:
        pass


class Deferred(LifecycleCapability):
    pass


class Complete(Settled):
    def assert_complete(self) -> None:
        """The outcome has been recorded; no uncertain work remains."""


class Successful(Terminal):
    @classmethod
    def success_count(cls) -> int:
        return 1
