"""The two independent read callbacks consumed by registry privacy."""
from abc import ABC, abstractmethod

from agent_comms.nominal.field_codec import AcpSessionId


class NativeAnswerRead[Reference](ABC):
    @abstractmethod
    def answered_inputs(self, references: tuple[Reference, ...]) -> frozenset[Reference]: ...

class CursorMetadataRead[Update](ABC):
    @abstractmethod
    async def observed_metadata(self, thread_name: str, session_id: AcpSessionId) -> tuple[Update, ...]: ...
