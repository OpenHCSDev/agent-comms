"""Lower the original history/context declarations without publication/store back-edges."""

import json

from p23_revision_owner_plan import SRC, imported, patch, stage

messages = SRC / "messaging/messages.py"
references = SRC / "display_references.py"
history = SRC / "historical_views.py"
old_refs = references.read_text()
new_refs = old_refs.replace("TYPE_CHECKING, ", "")
guarded = '''if TYPE_CHECKING:
    from agent_comms.historical_views import HistorySource
    from agent_comms.messaging.messages import MessageHistory
    from agent_comms.registry.registry_document import RegistrySnapshot
'''
if guarded not in new_refs:
    raise ValueError("The original display-reference guarded block changed")
new_refs = new_refs.replace(guarded, '''from agent_comms.page_traversal import HistorySourceKey
from agent_comms.registry.registry_names import RegistryNames
from agent_comms.storage.private_path import FileObservation
from agent_comms.registry.thread_identity_source import ThreadIdentitySource
''')
new_refs = new_refs.replace("source: HistorySource", "source: HistoryReferenceSource")
new_refs = new_refs.replace("registry: RegistrySnapshot", "registry: RegistryNames[ThreadIdentitySource]")
new_refs = new_refs.replace("class RegistryReferenceSource(ABC):", '''class HistoryReferenceSource(ABC):
    """The original recorded identity and names needed to link a history body."""

    __slots__ = ()
    provenance: RegistryNames[ThreadIdentitySource]

    @property
    @abstractmethod
    def key(self) -> HistorySourceKey: ...

    @property
    @abstractmethod
    def snapshot_identity(self) -> tuple[HistorySourceKey, int, FileObservation]: ...


class RegistryReferenceSource(ABC):''')
old_messages = messages.read_text()
new_messages = old_messages.replace("tuple[bus_publication.Delivered, ...]", "tuple[MessageDeliveryRead, ...]")
new_messages = new_messages.replace("display_references.ReferenceContext", "ReferenceContext")
old_tail = "from agent_comms import bus_publication, display_references, retained_task_facts as task_facts"
if old_tail not in new_messages:
    raise ValueError("The original concrete message-module tail changed")
new_messages = new_messages.replace(old_tail, "from agent_comms import retained_task_facts as task_facts")
new_messages = new_messages.replace("from __future__ import annotations\n", '''from __future__ import annotations

from agent_comms.display_references import ReferenceContext
from agent_comms.messaging.message_reads import MessageDeliveryRead
''', 1)
reason = (
    "messages imports bus_publication solely for BusCapability.deliveries' postponed return annotation. "
    "That makes audience_manifest -> messages -> bus_publication -> audience_manifest request AudiencedMessage before it exists. "
    "The actual Delivered already inherits MessageDeliveryRead; use that existing nominal result capability, leaving the empty return and actual delivered records unchanged. "
    "Keep the actual MessageHistory/NoHistorySource/default singleton and ReferenceContext together in display_references, below the historical archive, rather than import the live archive into message bootstrap. "
    "HistoryReferenceSource declares only the original provenance, key and snapshot_identity read facts used by DisplayReferences.recorded/for_history; the actual HistorySource inherits it and retains every original dataclass field and body. "
    "ReferenceNames.of and CurrentDisplayReferences.from_registry use existing RegistryNames[ThreadIdentitySource].named_incarnations, preserving the original calculation. "
    "No surrogate record, compatibility re-export, new loader, authority acquisition, SQL metadata getter or publication behavior is introduced."
)
plan = {"stages": [
    stage("p23-lower-original-reference-reads-and-remove-publication-annotation-edge", reason,
          [patch(references, [(old_refs, new_refs)]), patch(messages, [(old_messages, new_messages)])]),
    stage("p23-relocate-original-message-history-family-beside-reference-context", reason,
          [{"operation": "relocate_symbols_to_module", "file_path": str(messages),
            "destination_path": str(references),
            "symbol_qualnames": ["MessageHistory", "NoHistorySource", "NO_HISTORY_SOURCE"]}]),
    stage("p23-original-archive-implements-reference-read-capability", reason,
          [imported(history, "from agent_comms.display_references import HistoryReferenceSource")]),
    stage("p23-compose-original-archive-reference-capability", reason,
          [{"operation": "add_class_base", "file_path": str(history),
            "target_qualname": "HistorySource", "base_name": "HistoryReferenceSource"}]),
]}
print(json.dumps(plan, indent=2))
