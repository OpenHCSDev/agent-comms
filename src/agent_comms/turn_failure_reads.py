"""The original turn and launch facts consumed by failure classification."""

from abc import ABC, abstractmethod
from collections.abc import Collection
from sqlite3 import Connection

from agent_comms.planning.attempt_resolution import RecordedResolution


class ModelFailureDiagnostic[Failure](ABC):
    __slots__ = ()

    @abstractmethod
    def refine_model_request_failure(self, failure: Failure) -> Failure: ...


class TerminalPromptRead(ABC):
    __slots__ = ()

    started: bool


class TerminalInputsRead(ABC):
    __slots__ = ()

    uncertain: bool
    pending: Collection[object]

    @property
    @abstractmethod
    def unwritten_inputs(self) -> Collection[object]: ...


class TerminalOutputRead(ABC):
    __slots__ = ()

    error_message: str | None
    final_assistant_stop: bool

    @property
    @abstractmethod
    def failure_text(self) -> str: ...

    @property
    @abstractmethod
    def clean(self) -> bool: ...


class TerminalSessionRead(ABC):
    __slots__ = ()

    prompt_state: TerminalPromptRead
    inputs: TerminalInputsRead
    output: TerminalOutputRead


class ReplayPreparation(ABC):
    __slots__ = ()

    @classmethod
    @abstractmethod
    def revoke(cls, db: Connection, execution_id: str) -> None: ...

    @classmethod
    @abstractmethod
    def authorize_if_unobserved(cls, db: Connection, execution_id: str) -> None: ...


class GoalFailureReservation[Store, Observation](ABC):
    __slots__ = ()

    @abstractmethod
    def fail(
        self, store: Store, diagnostic: RecordedResolution, *,
        observation: Observation | None = ...,
    ) -> object: ...


class GoalFailurePermit[Store, Observation](ABC):
    __slots__ = ()

    reservation: GoalFailureReservation[Store, Observation]

    @abstractmethod
    def stop(self, store: Store) -> None: ...
