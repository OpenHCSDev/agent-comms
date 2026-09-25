# Pi MCP client (draft)

The Pi package manifest loads `index.mjs` **by default when the package is installed**.
It currently provides `/mcp-status`, `/mcp-approve <id>`, and `/mcp-deny <id>`;
actual server connections and Pi model tools are the next implementation slice.

```sh
cd extensions/pi-mcp-client
npm ci --ignore-scripts
npm test
```

The native config is `{"version":1,"servers":[...]}` with unique server IDs.
Each entry requires `id`, `enabled`, `instructionsPolicy:"status-only"`, and
`transport:{"type":"stdio","command":"...","args":[],"cwd":"project"}`.
Optional `env` supplies literal values; `envFrom` maps child variables to host
variable names. Unknown fields/transports fail. User config is
`getAgentDir()/mcp.json`; project config is `<cwd>/<CONFIG_DIR_NAME>/mcp.json`.
The project file is not read until Pi project trust is active. The package-owned
`getAgentDir()/mcp-trust.json` separately approves the *exact* canonical
project, scope, server ID and complete declaration digest. Changed declarations
need a new approval; project overlays never fall back to user commands.

Approval shows the complete command, arguments, root and digest in a local Pi
TUI confirmation without exposing literal environment values. RPC/headless
approval is refused until a correlated human-controller bridge exists. The
inert launch-spec builder rejects unapproved/mismatched project contexts and
constructs a narrow SDK environment with explicit `envFrom`, canonical cwd and
piped stderr. No transport is constructed or server launched by this builder.

The offline suite tests the official SDK's real stdio handshake, capability-
gated bounded tools/resources/prompts discovery, operations, progress,
cancellation and cleanup against a generic fixture. It also runs an isolated
real Pi RPC process with `--no-approve` and `--approve`: untrusted files are not
read, absent/stale digests cannot authorize a declaration, and no declared
server starts. No provider call is made. PR #77 remains draft; session lifecycle,
Pi tool exposure, agent-comms/ACP projection, Toad controls and their
independent review are still outstanding.
