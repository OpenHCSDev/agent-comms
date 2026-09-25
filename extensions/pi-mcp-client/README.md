# Pi MCP client — default-off control and contract probe (no server sessions yet)

This draft verifies the official MCP SDK's local stdio handshake,
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
server-ID, declaration-digest match from the external ledger. A small opt-in Pi
extension (`index.mjs`) offers `/mcp-status`, `/mcp-approve <id>`, and
`/mcp-deny <id>`. Approval shows the full command/arguments/digest, redacts
literal environment values, requires Pi project trust and an actual local TUI
confirmation, then writes the external ledger atomically. It rejects approval
in headless/RPC mode rather than assuming an RPC dialog has a human responder.
**No declared server is started** and no MCP tool is exposed to Pi. The package
manifest still has `pi.extensions=[]`; do not install or activate it in a live
project before independent review. There is no provider/model call, agent-comms
backend integration, Toad control, or OpenHCS-specific behavior.

The provisional native document uses `{"version":1,"servers":[...]}` so duplicate
server IDs can be rejected rather than hidden by JSON object parsing. Each entry
requires `id`, `enabled`, `instructionsPolicy:"status-only"`, and a `stdio`
transport with `command`, `args`, `cwd:"project"`, plus optional `env` and
`envFrom` maps. Unknown fields and transports fail closed. This is deliberately
smaller than the planned final config; do not mistake its digest for approval.
Do not install or advertise this as a usable MCP client. This probe deliberately
uses only the fixture's explicit command; never read untrusted project declarations
or launch one as a consequence of opening a project.

Next gate: independently review/freeze the source/ledger/approval contract.
The no-provider suite exercises **real isolated Pi RPC** with `--no-approve` and
`--approve`, with absent/present external digest, verifies refusal to approve in
headless mode, and confirms zero declared server spawns. The TUI approval dialog
still needs an executable human-UI test. Only after trust gates and independent
review may a package-owned session lifecycle, Pi tools, calls, results and
headless-safe status be added. Generic Pi RPC/ACP approval forwarding and Toad
remain subsequent separately reviewed slices.
