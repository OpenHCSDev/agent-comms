"""One complete acquisition order, loaded before store instances open."""
from .registry_store import RegistryStore
from .catalog_store import ChannelCatalog
from .read_ledger import ReadLedger
from .relationships import RelationshipStore
from .runtime_info import RuntimeInfoStore
from .runtime_children import RuntimeChildrenStore
from .root_runtime import RootRuntimeStore
from .shared_ledger import SharedLedger
from .collaboration_ledger import CollaborationLedger
from .connector_store import ConnectorStore
from .service_passes import ServicePassStore
from .store_files import StoreLockOwner

STORE_LOCK_ORDER = (
    RegistryStore, ChannelCatalog, ReadLedger,
    RelationshipStore, RuntimeInfoStore, SharedLedger, CollaborationLedger, ConnectorStore,
    RuntimeChildrenStore, RootRuntimeStore, ServicePassStore,
)

StoreLockOwner.declare_lock_order(STORE_LOCK_ORDER)
