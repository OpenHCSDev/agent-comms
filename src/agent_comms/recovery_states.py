"""Recovery incidents decide whether auditing may run; settlement handles terminal incidents."""

from abc import abstractmethod
from dataclasses import dataclass

from .coordination_errors import IdentityConflict
from .declared_family import DeclaredFamily
from .lifecycle_capability import LifecycleCapability, Unresolved, Active


@dataclass(frozen=True)
class RecoveryCondition(LifecycleCapability, DeclaredFamily, affix="Recovery"):

    @classmethod
    @abstractmethod
    def validate_audit(cls, snapshot, attempt) -> None: ...


class IncidentRecovery(Unresolved):

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
            not snapshot.execution.lifecycle.items_with(Active, snapshot.execution)
            or not attempt.lifecycle.running
            or incident is None
            or incident.attempt != attempt.attempt_ordinal
            or not incident.kind.items_with(Unresolved, incident)
        ):
            raise IdentityConflict(
                "recovered requires a current unresolved incident and resumed model"
            )
