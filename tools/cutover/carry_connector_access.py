"""Carry live connector authorities into one declaration-owned bearer family.

Only this explicit installer step accepts the retired split access/thread format.
The running ConnectorStore has one format and no compatibility decoder.
"""

from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import JsonValue
from yarl import URL

from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.storage.locked_store import RootFile
from agent_comms.storage.store_files import AtomicFile
from agent_comms.transport.connector_store import (
    AccessLabel,
    AuthorizationRequest,
    ConnectorAccess,
    ConnectorCredentialsFile,
    ConnectorGrants,
    ExistingDirectorAccess,
    ExpiringGrant,
    OAuthRedirectUri,
    PublicOAuthClient,
    RegisteredClient,
    ThreadGrant,
    TrustedBrowser,
)


PREVIOUS_CLAUDE_CALLBACK = OAuthRedirectUri(URL("https://claude.ai/api/mcp/auth_callback"))


@dataclass(frozen=True)
class PreviousIssuedAccess(ExpiringGrant):
    client_id: AccessLabel

    def carried(self) -> ExistingDirectorAccess:
        # OAuth and local proofs both occupied this record. Preserve its label and
        # Director authority without inventing which producer issued the bearer.
        return ExistingDirectorAccess(self.client_id, expires_at=self.expires_at)


@dataclass(frozen=True)
class PreviousRegisteredClient:
    """The retired connector registered only Claude and retained only its display name."""

    name: str

    def carried(self) -> RegisteredClient:
        return RegisteredClient(self.name, (PREVIOUS_CLAUDE_CALLBACK,), PublicOAuthClient())


@dataclass(frozen=True)
class PreviousThreadGrant:
    thread: str

    def carried(self) -> ThreadGrant:
        return ThreadGrant(self.thread)


@dataclass(frozen=True, kw_only=True)
class PreviousIssuedCode(ExpiringGrant):
    """A pre-redirect-binding code; its five-minute authority is dropped during cutover."""

    client_id: str
    code_challenge: str


@dataclass(frozen=True)
class PreviousConnectorGrants:
    """The actual retired input; its clients, waits, codes and trust stay unchanged."""

    clients: dict[str, PreviousRegisteredClient] = field(default_factory=dict)
    requests: dict[str, AuthorizationRequest] = field(default_factory=dict)
    codes: dict[str, PreviousIssuedCode] = field(default_factory=dict)
    access: dict[str, PreviousIssuedAccess] = field(default_factory=dict)
    threads: dict[str, PreviousThreadGrant] = field(default_factory=dict)
    refresh: dict[str, str] = field(default_factory=dict)
    trusted_browsers: dict[str, TrustedBrowser] = field(default_factory=dict)

    @classmethod
    def needed(cls, document: dict[str, JsonValue]) -> bool:
        try:
            FieldCodec.decode(ConnectorGrants, document)
        except MalformedRecord:
            pass
        else:
            return False
        if FieldCodec.record_key(cls, "threads") in document:
            return True
        records = FieldCodec.decode(
            dict[str, dict[str, JsonValue]],
            document.get(FieldCodec.record_key(cls, "access"), {}),
        )
        return any(ConnectorAccess.family_discriminator not in record for record in records.values()) \
            or bool(document.get(FieldCodec.record_key(cls, "codes"), {}))

    def carried(self) -> ConnectorGrants:
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
        return ConnectorGrants(
            clients={client: registered.carried() for client, registered in self.clients.items()},
            requests=self.requests,
            codes={},
            access=access,
            refresh=self.refresh,
            trusted_browsers=self.trusted_browsers,
        )


class ConnectorAccessCarry(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = ()
    writer_owned = (ConnectorCredentialsFile,)

    def carry(self, *, apply: bool) -> Carried:
        resource, = ConnectorCredentialsFile.resources(self.files)
        path = self.files.location(resource)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        with ConnectorCredentialsFile.write_guard(path):
            document = FieldCodec.decode(
                dict[str, JsonValue], FieldCodec.parse(path.read_text(encoding="utf-8")))
            if not PreviousConnectorGrants.needed(document):
                FieldCodec.decode(ConnectorGrants, document)
                return Carried(0)
            previous = FieldCodec.decode(PreviousConnectorGrants, document)
            successor = previous.carried()
            encoded = FieldCodec.encode(successor)
            FieldCodec.decode(ConnectorGrants, encoded)
            if apply:
                AtomicFile(path).write_text(FieldCodec.compact(encoded, allow_nan=False))
            return Carried(
                1,
                detail={"access": len(previous.access), "threads": len(previous.threads)},
                rewritten=(resource.path,) if apply else (),
            )
