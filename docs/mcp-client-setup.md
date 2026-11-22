# Connect an MCP host to agent-comms

The remote MCP resource is the connector's public HTTPS URL ending in `/mcp`.
For this host it is currently:

```text
https://ts-gaming.tail553874.ts.net/mcp
```

The connector publishes protected-resource and authorization-server metadata. It
supports OAuth authorization code with PKCE, dynamic client registration, native
loopback callbacks, HTTPS callbacks, refresh tokens, and issuer-bound callbacks.

## Hosts with automatic OAuth registration

Give the host only the MCP URL and enable OAuth discovery. The host discovers the
authorization and registration endpoints, registers its own callback, opens the
browser, and exchanges the code. Claude and Gemini CLI both fit this path; neither
needs a client secret.

Gemini CLI user configuration (`~/.gemini/settings.json`):

```json
{
  "mcpServers": {
    "agent-comms": {
      "url": "https://ts-gaming.tail553874.ts.net/mcp",
      "type": "http",
      "oauth": {"enabled": true, "allowPrivateNetwork": true}
    }
  }
}
```

Then run `/mcp auth agent-comms` and `/mcp list`.

`allowPrivateNetwork` is for a machine on the tailnet: there the connector's name
resolves to its tailnet address (`100.x`), which Gemini CLI treats as a private
network and refuses to sign in to ("resolves to private network address ... which is
blocked") unless this is set. Off the tailnet the name resolves to a public address
and the setting changes nothing.

## A client that takes few tools

`/mcp` lists every tool, about 120 of them and half a megabyte of schema. The Gemini
app's custom-app form signs in, reads that list and stops ("Account linking is required
to use this custom app", 2026-11-22). Give such a client the same address with `/short`
on the end:

```
https://<connector host>/mcp/short
```

Sign-in and the grant are the same. The list is seven tools: send, inbox, history,
channels, threads, `comms_find_tool` (search every other tool and read what it takes)
and `comms_call_tool` (run one by name). Nothing is out of reach, and `/mcp` is unchanged.

## Hosts that require a client ID or secret

First copy the exact callback URL displayed by that host. Register it locally on
the connector machine:

```sh
agent-comms-connector register-client \
  --name 'Gemini Connected App' \
  --redirect-uri 'https://client.example/oauth/callback' \
  --confidential
```

The command prints the client ID and its secret once. The connector stores only
the secret digest. Enter those values in the host together with the connector's
authorization and token endpoints:

```text
https://ts-gaming.tail553874.ts.net/authorize
https://ts-gaming.tail553874.ts.net/token
```

Omit `--confidential` for a public PKCE client; its secret is empty.

## Hosts that accept an Authorization header

OAuth is preferable because it refreshes automatically. A client that supports
custom HTTP headers can instead use a separately scoped, expiring UI grant from
`agent-comms-connector grant-ui` and send its private profile token as:

```text
Authorization: Bearer TOKEN
```

This is a distinct grant path. It does not create an OAuth client and should not
be copied into project configuration or shell history.
