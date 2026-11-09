"""The original turn and launch facts consumed by failure classification."""

from collections.abc import Collection
from sqlite3 import Connection
from typing import Protocol

from agent_comms.attempt_resolution import RecordedResolution


class ModelFailureDiagnostic[Failure](Protocol):
    def refine_model_request_failure(self, failure: Failure) -> Failure: ...


class TerminalPromptRead(Protocol):
    @property
    def started(self) -> bool: ...


class TerminalInputsRead(Protocol):
    @property
    def uncertain(self) -> bool: ...

    @property
    def pending(self) -> Collection[object]: ...

    @property
    def unwritten_inputs(self) -> Collection[object]: ...


class TerminalOutputRead(Protocol):
    @property
    def failure_text(self) -> str: ...

    @property
    def error_message(self) -> str | None: ...

    @property
    def final_assistant_stop(self) -> bool: ...

    @property
    def clean(self) -> bool: ...


class TerminalSessionRead(Protocol):
    @property
    def prompt_state(self) -> TerminalPromptRead: ...

    @property
    def inputs(self) -> TerminalInputsRead: ...

    @property
    def output(self) -> TerminalOutputRead: ...


class ReplayPreparation(Protocol):
    @staticmethod
    def revoke(db: Connection, execution_id: str) -> None: ...

    @staticmethod
    def authorize_if_unobserved(db: Connection, execution_id: str) -> None: ...


class GoalFailureReservation[Store, Observation](Protocol):
    def fail(
        self, store: Store, diagnostic: RecordedResolution, *,
        observation: Observation | None = ...,
    ) -> object: ...


class GoalFailurePermit[Store, Observation](Protocol):
    @property
    def reservation(self) -> GoalFailureReservation[Store, Observation]: ...

    def stop(self, store: Store) -> None: ...
