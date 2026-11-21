"""Bind pre-declaration connector clients to their actual OAuth callback and method."""

from yarl import URL
from pydantic import JsonValue

from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.transport.connector_store import (
    ConnectorGrants,
    OAuthRedirectUri,
    PublicOAuthClient,
    RegisteredClient,
)
from cutover.carry_connector_access import (
    AccessCarriedConnectorGrants,
    ConnectorCredentialsCarry,
    ConnectorCredentialsConversion,
    ConvertedConnectorCredentials,
    CurrentConnectorCredentials,
)


PREVIOUS_CLAUDE_CALLBACK = OAuthRedirectUri(URL("https://claude.ai/api/mcp/auth_callback"))


class ConnectorOAuthClients(ConnectorCredentialsCarry):
    """The one transition from name-only clients to declared OAuth client facts."""

    def conversion(self, document: dict[str, JsonValue]) -> ConnectorCredentialsConversion:
        try:
            FieldCodec.decode(ConnectorGrants, document)
        except MalformedRecord:
            pass
        else:
            return CurrentConnectorCredentials()
        previous = FieldCodec.decode(AccessCarriedConnectorGrants, document)
        current = ConnectorGrants(
            clients={client: RegisteredClient(
                registered.name, (PREVIOUS_CLAUDE_CALLBACK,), PublicOAuthClient(),
            ) for client, registered in previous.clients.items()},
            requests=previous.requests,
            codes={},
            access=previous.access,
            refresh=previous.refresh,
            trusted_browsers=previous.trusted_browsers,
        )
        encoded = FieldCodec.encode(current)
        FieldCodec.decode(ConnectorGrants, encoded)
        return ConvertedConnectorCredentials(encoded, {
            "clients": len(previous.clients), "expired_codes": len(previous.codes),
        })
