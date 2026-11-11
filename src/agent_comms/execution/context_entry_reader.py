"""Read original context manifests by their native entry identities."""
from abc import ABC, abstractmethod
from collections.abc import Iterable
from agent_comms.execution.native_session_reopen import NativeSessionIdentity
from agent_comms.execution.turn_context import SegmentManifest
from agent_comms.execution.turn_item import NativeItemId

class ReadsContextEntries(ABC):
    @abstractmethod
    def entries(self, identity: NativeSessionIdentity, entry_ids: Iterable[NativeItemId]) -> dict[NativeItemId, SegmentManifest]: ...
