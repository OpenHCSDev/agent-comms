"""Recovery incidents own audit admission; terminal incidents belong to settlement."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .coordination_errors import IdentityConflict
from .declared_family import DeclaredFamily


@dataclass(frozen=True)
class RecoveryCondition(DeclaredFamily, affix="Recovery"):
    unresolved: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def validate_audit(cls, snapshot, attempt) -> None: ...


class IncidentRecovery:
    unresolved = True

    @classmethod
    def validate_audit(cls, snapshot, attempt):
        if cls.declared_name != attempt.lifecycle.declared_name:
            raise IdentityConflict("recovery audit must match observed attempt phase")


class ModelStalledRecovery(IncidentRecovery, RecoveryCondition):
    pass


class AbortingRecovery(IncidentRecovery, RecoveryCondition):
    pass


class RetryingRecovery(IncidentRecovery, RecoveryCondition):
    pass


class ProviderUnavailableRecovery(IncidentRecovery, RecoveryCondition):
    pass


class DeferredRecovery(RecoveryCondition):
    @classmethod
    def validate_audit(cls, snapshot, attempt):
        raise IdentityConflict("terminal audit belongs to atomic settlement")


class FailedRecovery(DeferredRecovery):
    pass


class RecoveredRecovery(RecoveryCondition):
    @classmethod
    def validate_audit(cls, snapshot, attempt):
        incident = snapshot.last_recovery
        if (
            not snapshot.execution.lifecycle.active
            or not attempt.lifecycle.running
            or incident is None
            or incident.attempt != attempt.attempt_ordinal
            or not incident.kind.declaration.unresolved
        ):
            raise IdentityConflict(
                "recovered requires a current unresolved incident and resumed model"
            )
