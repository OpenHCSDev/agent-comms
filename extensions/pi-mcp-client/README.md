# Pi MCP client — contract probe (not functional yet)

This draft's first slice verifies the official MCP SDK's local stdio handshake,
capability-gated tools/resources/prompts discovery, cursor pagination, calls,
progress, cancellation, and child cleanup against a generic fixture server. `discover()` rejects a
truncated/repeated/invalid catalog instead of presenting it as complete.

```sh
cd extensions/pi-mcp-client
npm ci --ignore-scripts
npm test
```

**Default off:** the Pi package manifest loads no extension. A strict inert
version-1 native declaration parser and canonical declaration digest are present.
An inert loader now reads user `mcp.json` and separate `mcp-trust.json` from a
Pi-owned agent directory, and project `<CONFIG_DIR_NAME>/mcp.json` **only** when
`ctx.isProjectTrusted()` is true. Approval requires an exact project-realpath,
server-ID, declaration-digest match from the external ledger. It derives status,
but does not write/approve anything or launch any server. It is not loaded by Pi;
no exposed Pi tools, provider/model call, agent-comms backend integration, Toad
control, or OpenHCS-specific behavior is implemented.

The provisional native document uses `{"version":1,"servers":[...]}` so duplicate
server IDs can be rejected rather than hidden by JSON object parsing. Each entry
requires `id`, `enabled`, `instructionsPolicy:"status-only"`, and a `stdio`
transport with `command`, `args`, `cwd:"project"`, plus optional `env` and
`envFrom` maps. Unknown fields and transports fail closed. This is deliberately
smaller than the planned final config; do not mistake its digest for approval.
Do not install or advertise this as a usable MCP client. This probe deliberately
uses only the fixture's explicit command; never read untrusted project declarations
or launch one as a consequence of opening a project.

Next gate: independently review/freeze the native config, Pi-trust-gated read
contract and external ledger schema; add a user-authorized, durable ledger writer
and a Pi extension that cannot start sessions unless eligibility is approved.
The current tests cover zero SDK transports/spawns on changed/untrusted project
commands; actual Pi-context and process-boundary tests remain outstanding. Only then wire a package-owned
session lifecycle, calls, results, and headless-safe status. Generic Pi RPC/ACP
approval forwarding and Toad remain subsequent separately reviewed slices.
