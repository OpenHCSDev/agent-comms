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

**Default off:** the Pi package manifest loads no extension. No user/project
configuration, approval ledger, exposed Pi tools, provider/model call, agent-comms
backend integration, Toad control, or OpenHCS-specific behavior is implemented.
Do not install or advertise this as a usable MCP client. This probe deliberately
uses only the fixture's explicit command; never read untrusted project declarations
or launch one as a consequence of opening a project.

Next gate: freeze a strict native config and independent exact-digest approval
contract using Pi's project-trust authority; test changed/untrusted project
commands cause zero SDK transports/spawns. Only then wire a package-owned
session lifecycle, calls, results, and headless-safe status. Generic Pi RPC/ACP
approval forwarding and Toad remain subsequent separately reviewed slices.
