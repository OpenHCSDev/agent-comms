"""Recovery incidents recorded against an attempt; settlement handles terminal incidents."""

from dataclasses import dataclass

from .coordinator_vocabulary import CoordinatorVocabulary
from .lifecycle_capability import LifecycleCapability, Unresolved


@dataclass(frozen=True)
class RecoveryCondition(LifecycleCapability, CoordinatorVocabulary, affix="Recovery"):
    @classmethod
    def sql_vocabulary(cls) -> dict[str, str]:
        return dict(recovery_names=cls.sql_values(cls.names()))


class IncidentRecovery(Unresolved):
    pass


class ModelStalledRecovery(IncidentRecovery, RecoveryCondition):
    pass


class AbortingRecovery(IncidentRecovery, RecoveryCondition):
    pass


class RetryingRecovery(IncidentRecovery, RecoveryCondition):
    pass


class ProviderUnavailableRecovery(IncidentRecovery, RecoveryCondition):
    pass


class DeferredRecovery(RecoveryCondition):
    pass


class FailedRecovery(DeferredRecovery):
    pass


class RecoveredRecovery(RecoveryCondition):
    pass
