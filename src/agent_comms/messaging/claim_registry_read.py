"""Original acquired-registry callbacks used to validate resource claims."""

from abc import abstractmethod
from collections.abc import Iterable, Iterator

from agent_comms.registry.presence_reads import PresenceThread
from agent_comms.registry.claim_generation_read import ClaimGenerationRead
from agent_comms.registry.thread_identity import RecordedThreadIncarnation


class ClaimRegistryRead[Owner: PresenceThread](ClaimGenerationRead):
    __slots__ = ()

    @abstractmethod
    def threads_for(self, incarnation: RecordedThreadIncarnation) -> tuple[Owner, ...]: ...

    @abstractmethod
    def active_threads(self, candidates: Iterable[Owner]) -> Iterator[Owner]: ...
