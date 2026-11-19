"""Which registry roots are private, and what a private root's native namespace scopes.

A private root records its namespace as a row of the registry's own file (RegistryNamespace),
written once when the root becomes private. A root with no namespace is public, unless its
coordinator already holds native tables, which is incomplete state, never a new namespace.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from abc import abstractmethod
from pathlib import Path
from typing import NoReturn

from agent_comms.coordination.cohort_schema import AcceptanceFrontier, PrivateCoordinatorSchema
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import PublicationActivationBlocked
from agent_comms.coordination.private_runtime_schema import PrivateCoordinatorTable
from agent_comms.coordination.schema import CoordinatorTable
from agent_comms.coordination.tables.participants import Participants
from agent_comms.coordination.tables.response_schema import ResponseTable
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import Platform
from agent_comms.execution.input_root_authority import InputRootAuthority
from agent_comms.execution.native_input_owner import NativeOwnerRegistry, RegistryOwner
from agent_comms.execution.native_runtime_input import NativeRuntimeInput, NativeRuntimeTable
from agent_comms.execution.private_nk_entrypoint import (
    PrivateNkLaunch,
    RootLaunch,
    SharedRootLaunch,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.field_codec import AcpSessionId, RootId, ThreadLookup
from agent_comms.registry.notification_selection_read import NotificationSelectionRead
from agent_comms.registry.privacy_reads import CursorMetadataRead, NativeAnswerRead
from agent_comms.registry.registry_tables import RegistryNamespace, RegistryRowStore
from agent_comms.registry.thread_identity import ClaimedIdentity
from agent_comms.storage.file_observation import FileObservation
from agent_comms.storage.typed_table import TableSchema
from agent_comms.transport.attachment_scope import CursorScope, LiveCursorScope, NoCursorScope


def _reject(detail: str) -> NoReturn:
    raise RelationViolationError(f"Private registry {detail}")


class RegistryPrivacy(InputRootAuthority):
    """What a registry root's protocol marker decides, selected by the registry store."""

    def evidence(self, namespace: tuple[RegistryNamespace, ...],
                 coordinator: FileObservation) -> tuple[object, ...]:
        """What this answer was decided from. A public root is decided by the namespace's
        absence and the coordinator's schema."""
        return namespace, coordinator

    def native_answered(self, answers: NativeAnswerRead[MessageReference],
                        references: tuple[MessageReference, ...]) -> frozenset[MessageReference]:
        """Which of these inputs a native transcript answered, read under this root's privacy."""
        return answers.answered_inputs(references)

    @abstractmethod
    def cursor_scope(
        self, snapshot: NativeOwnerRegistry, thread_name: str, session_id: str,
    ) -> CursorScope: ...

    @abstractmethod
    async def cursor_metadata[Update](
        self, publication: CursorMetadataRead[Update], thread_name: str, session_id: AcpSessionId,
    ) -> tuple[Update, ...]: ...

    @abstractmethod
    def launch(self, root: Path) -> RootLaunch: ...

    @abstractmethod
    def prepare_native(self, registry_path: Path) -> PrivateRegistry: ...

    @abstractmethod
    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        """Context evidence for every row in the captured assignment window ``w``."""

    @abstractmethod
    def context_entered_sql(self, assignment: str) -> str:
        """Whether a native input committed ``assignment``'s message to its session context."""

    @abstractmethod
    def missing_notification_assignments[Reply](
        self, selection: NotificationSelectionRead[Reply],
    ) -> Reply: ...

    @classmethod
    @abstractmethod
    def coordinator_schema(cls) -> type[TableSchema]:
        """The table family declared by this protocol kind."""

    @classmethod
    def declared_schema_at(cls, registry_path: Path) -> type[TableSchema]:
        """Read a schema declaration without constructing a usable registry capability."""
        return cls.coordinator_schema()

    @classmethod
    @abstractmethod
    def load(cls, registry_path: Path) -> RegistryPrivacy:
        """Load the root's usable privacy capability, including its data commitment checks."""

    @abstractmethod
    def declare_acceptance(self, db: sqlite3.Connection, recipient_lookup: ThreadLookup) -> None:
        """Prepare the new recipient's acceptance state before registry publication."""

    @abstractmethod
    def publication_connection(self, connection: sqlite3.Connection) -> sqlite3.Connection | None:
        """This root's recorded native publications, or no private publication source."""


class PublicRegistry(RegistryPrivacy):

    def declare_acceptance(self, db: sqlite3.Connection, recipient_lookup: ThreadLookup) -> None:
        """Public roots have no native cohort acceptance state."""

    def publication_connection(self, connection: sqlite3.Connection) -> None:
        """Public attached history has no private native publication relations."""

    @classmethod
    def coordinator_schema(cls) -> type[TableSchema]:
        CoordinatorTable.declarations()
        return CoordinatorTable

    @classmethod
    def load(cls, registry_path: Path) -> PublicRegistry:
        return cls()

    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        return (
            "SELECT assignment_id,0 AS triage_inflight,"
            "NULL AS context_committed_at_ms FROM w"
        )

    def context_entered_sql(self, assignment: str) -> str:
        """A public root has no native inputs, so no native context entry."""
        return "0"

    def missing_notification_assignments[Reply](
        self, selection: NotificationSelectionRead[Reply],
    ) -> Reply:
        return selection.read_without_assignments()

    def cursor_scope(
        self, snapshot: NativeOwnerRegistry, thread_name: str, session_id: str,
    ) -> CursorScope:

        return NoCursorScope()

    async def cursor_metadata[Update](
        self, publication: CursorMetadataRead[Update], thread_name: str, session_id: AcpSessionId,
    ) -> tuple[Update, ...]:
        return ()

    def native_answered(self, answers: NativeAnswerRead[MessageReference],
                        references: tuple[MessageReference, ...]) -> frozenset[MessageReference]:
        """A public root has no native inputs, so no native answers."""
        return frozenset()

    @property
    def namespace(self) -> RootId:
        _reject("native namespace requires the original registry guard")

    def launch(self, root: Path) -> RootLaunch:
        return SharedRootLaunch(root)

    def prepare_native(self, registry_path: Path) -> PrivateRegistry:
        guard = PrivateRegistry(registry_path, RootId.mint())
        guard._trusted_root()
        # Install the new private capabilities while the existing public schema
        # still owns opening this file, before publishing the private guard.
        # Participants registered while public declared no acceptance; the new
        # root declares it for each, as registration does for later ones.
        with coordinator.Coordination(str(CoordinationStore.path_in(registry_path.parent))) as store:
            PrivateCoordinatorSchema.install(store)
            with store.session.writing() as db:
                for participant in Participants.select(db):
                    guard.declare_acceptance(db, participant.participant_lookup)
        RegistryRowStore(registry_path.parent).write_namespace(guard.namespace)
        return guard

    def require_root(self, root_id: RootId, error: Exception) -> None:
        raise error


class PrivateRegistry(RegistryPrivacy):
    """A private root: its namespace row scopes its native inputs and cursors."""

    def evidence(self, namespace: tuple[RegistryNamespace, ...],
                 coordinator: FileObservation) -> tuple[object, ...]:
        """A private root is decided by its namespace row alone."""
        return (namespace,)

    def declare_acceptance(self, db: sqlite3.Connection, recipient_lookup: ThreadLookup) -> None:
        AcceptanceFrontier(
            wire_root_id=self.namespace, recipient_lookup=recipient_lookup, through_seq=0,
        ).insert_if_absent(db)

    def publication_connection(self, connection: sqlite3.Connection) -> sqlite3.Connection:

        NativeRuntimeTable.require_installed(connection)
        ResponseTable.require_installed(connection)
        return connection

    @classmethod
    def coordinator_schema(cls) -> type[TableSchema]:
        PrivateCoordinatorTable.declarations()
        return PrivateCoordinatorSchema

    def prepare_runtime(self) -> None:
        PrivateCoordinatorTable.declarations()
        with coordinator.Coordination(str(CoordinationStore.path_in(self.registry_path.parent))) as store:
            PrivateCoordinatorSchema.install(store)

    def notification_context_sql(self, connection: sqlite3.Connection) -> str:
        NativeRuntimeTable.require_installed(connection)
        return NativeRuntimeInput.notification_context_sql()

    def context_entered_sql(self, assignment: str) -> str:
        members = f"({NativeRuntimeInput.source_membership_sql()})"
        return f"EXISTS (SELECT 1 {NativeRuntimeInput.context_entered_from(members, assignment)})"

    def missing_notification_assignments[Reply](
        self, selection: NotificationSelectionRead[Reply],
    ) -> Reply:
        raise PublicationActivationBlocked("Private notification coordinator is missing")

    def __init__(self, registry_path: Path, wire_root_id: RootId):
        self.registry_path = registry_path
        self.root_id = wire_root_id

    @classmethod
    def load(cls, registry_path: Path) -> PrivateRegistry:
        """The root's recorded namespace."""
        (row,) = RegistryRowStore(registry_path.parent).namespace()
        return cls(registry_path, row.root_id)

    @property
    def namespace(self) -> RootId:
        """The existing native protocol namespace, never the SQLite bus identity."""
        return self.root_id

    def cursor_scope(
        self, snapshot: NativeOwnerRegistry, thread_name: str, session_id: str,
    ) -> CursorScope:

        try:
            owner = RegistryOwner.capture_local(snapshot, thread_name)
        except RelationViolationError:
            return NoCursorScope()
        return LiveCursorScope(
            session_id=session_id, wire_root_id=self.namespace,
            claimed_identity=ClaimedIdentity(owner.thread.incarnation, owner.claimed_generation),
            owner=owner.thread.require_process(),
        )

    async def cursor_metadata[Update](
        self, publication: CursorMetadataRead[Update], thread_name: str, session_id: AcpSessionId,
    ) -> tuple[Update, ...]:
        return await publication.observed_metadata(thread_name, session_id)

    def launch(self, root: Path) -> RootLaunch:
        return PrivateNkLaunch.installed(root, self.namespace)

    def prepare_native(self, registry_path: Path) -> PrivateRegistry:
        if registry_path != self.registry_path:
            _reject("belongs to another registry")
        return self

    def require_root(self, root_id: RootId, error: Exception) -> None:
        if root_id != self.namespace:
            raise error

    def _trusted_root(self) -> None:
        if ".." in self.registry_path.parts:
            _reject("has an ambiguous root path")
        root = self.registry_path.parent.absolute()
        cursor = root
        platform = Platform.current()
        while True:
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                _reject("has a missing directory ancestor")
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(info.st_mode)
                or not platform.trusted_ancestor_owner(info.st_uid)
                or (platform.ancestor_write_bits(info) and not sticky_root)
            ):
                _reject("has an untrusted directory ancestor")
            if cursor == root and (
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            ):
                _reject("root must be owner-only")
            if cursor == cursor.parent:
                return
            cursor = cursor.parent


import agent_comms.coordination.coordinator as coordinator
