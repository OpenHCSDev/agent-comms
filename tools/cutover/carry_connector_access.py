"""Carry live connector authorities into one declaration-owned bearer family.

Only this explicit installer step accepts the retired split access/thread format.
The running ConnectorStore has one format and no compatibility decoder.
"""

from dataclasses import dataclass, field
from abc import abstractmethod
from pathlib import Path
from typing import ClassVar

from pydantic import JsonValue

from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.storage.locked_store import FileResource, RootFile
from agent_comms.storage.store_files import AtomicFile
from agent_comms.transport.connector_store import (
    AccessLabel,
    AuthorizationRequest,
    ConnectorAccess,
    ConnectorCredentialsFile,
    ExistingDirectorAccess,
    ExpiringGrant,
    ThreadGrant,
    TrustedBrowser,
)


@dataclass(frozen=True)
class PreviousIssuedAccess(ExpiringGrant):
    client_id: AccessLabel

    def carried(self) -> ExistingDirectorAccess:
        # OAuth and local proofs both occupied this record. Preserve its label and
        # Director authority without inventing which producer issued the bearer.
        return ExistingDirectorAccess(self.client_id, expires_at=self.expires_at)


@dataclass(frozen=True)
class PreviousThreadGrant:
    thread: str

    def carried(self) -> ThreadGrant:
        return ThreadGrant(self.thread)


@dataclass(frozen=True)
class PreviousRegisteredClient:
    name: str


@dataclass(frozen=True, kw_only=True)
class PreviousIssuedCode(ExpiringGrant):
    client_id: str
    code_challenge: str


@dataclass(frozen=True)
class AccessCarriedConnectorGrants:
    """The historical step's exact output; a later step owns later OAuth declarations."""

    clients: dict[str, PreviousRegisteredClient] = field(default_factory=dict)
    requests: dict[str, AuthorizationRequest] = field(default_factory=dict)
    codes: dict[str, PreviousIssuedCode] = field(default_factory=dict)
    access: dict[str, ConnectorAccess] = field(default_factory=dict)
    refresh: dict[str, str] = field(default_factory=dict)
    trusted_browsers: dict[str, TrustedBrowser] = field(default_factory=dict)


class ConnectorCredentialsConversion:
    """One cutover's answer for the selected credential document."""

    @abstractmethod
    def carried(self, path: Path, resource: FileResource, apply: bool) -> Carried: ...


@dataclass(frozen=True)
class CurrentConnectorCredentials(ConnectorCredentialsConversion):
    def carried(self, path: Path, resource: FileResource, apply: bool) -> Carried:
        return Carried(0)


@dataclass(frozen=True)
class ConvertedConnectorCredentials(ConnectorCredentialsConversion):
    encoded: JsonValue
    detail: dict[str, int | bool]

    def carried(self, path: Path, resource: FileResource, apply: bool) -> Carried:
        if apply:
            AtomicFile(path).write_text(FieldCodec.compact(self.encoded, allow_nan=False))
        return Carried(
            1, detail=self.detail, rewritten=(resource.path,) if apply else (),
        )


class ConnectorCredentialsCarry(Carry):
    """Shared file selection, confinement, locking and publication for connector cutovers."""

    opened_files: ClassVar[tuple[type[RootFile], ...]] = ()
    writer_owned = (ConnectorCredentialsFile,)

    @abstractmethod
    def conversion(self, document: dict[str, JsonValue]) -> ConnectorCredentialsConversion: ...

    def carry(self, *, apply: bool) -> Carried:
        resource, = ConnectorCredentialsFile.resources(self.files)
        path = self.files.location(resource)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        with ConnectorCredentialsFile.write_guard(path):
            document = FieldCodec.decode(
                dict[str, JsonValue], FieldCodec.parse(path.read_text(encoding="utf-8")),
            )
            return self.conversion(document).carried(path, resource, apply)


@dataclass(frozen=True)
class PreviousConnectorGrants(AccessCarriedConnectorGrants):
    """The actual retired input; its clients, waits, codes and trust stay unchanged."""

    access: dict[str, PreviousIssuedAccess] = field(default_factory=dict)
    threads: dict[str, PreviousThreadGrant] = field(default_factory=dict)

    @classmethod
    def needed(cls, document: dict[str, JsonValue]) -> bool:
        if FieldCodec.record_key(cls, "threads") in document:
            return True
        records = FieldCodec.decode(
            dict[str, dict[str, JsonValue]],
            document.get(FieldCodec.record_key(cls, "access"), {}),
        )
        return any(ConnectorAccess.family_discriminator not in record for record in records.values())

    def carried(self) -> AccessCarriedConnectorGrants:
        # The old resolver preferred a live access record, then the thread record.
        # A colliding digest therefore has time-dependent authority: merging by dict
        # overwrite would silently change it. Refuse, preserving all original bytes.
        if self.access.keys() & self.threads.keys():
            raise RelationViolationError(
                "Access and thread grants share a bearer digest; an owner authority decision is required")
        access: dict[str, ConnectorAccess] = {
            key: grant.carried() for key, grant in self.access.items()
        }
        access.update({key: grant.carried() for key, grant in self.threads.items()})
        return AccessCarriedConnectorGrants(
            clients=self.clients,
            requests=self.requests,
            codes=self.codes,
            access=access,
            refresh=self.refresh,
            trusted_browsers=self.trusted_browsers,
        )


class ConnectorAccessCarry(ConnectorCredentialsCarry):
    def conversion(self, document: dict[str, JsonValue]) -> ConnectorCredentialsConversion:
        if not PreviousConnectorGrants.needed(document):
            FieldCodec.decode(AccessCarriedConnectorGrants, document)
            return CurrentConnectorCredentials()
        previous = FieldCodec.decode(PreviousConnectorGrants, document)
        successor = previous.carried()
        encoded = FieldCodec.encode(successor)
        FieldCodec.decode(AccessCarriedConnectorGrants, encoded)
        return ConvertedConnectorCredentials(encoded, {
            "access": len(previous.access), "threads": len(previous.threads),
        })
