# Pi MCP client (draft)

The Pi package manifest loads `index.mjs` **by default when the package is installed**.
It connects approved stdio servers when a Pi session starts, discovers tools,
resources and prompts, registers bounded discovered Pi tools, and closes children
on session shutdown. It provides `/mcp-status`, `/mcp-approve <id>`, and
`/mcp-deny <id>`. Tool calls require a local Pi TUI confirmation; detached/RPC
tool calls fail until a correlated controller or out-of-band tool policy is added.

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
launch-spec builder rejects unapproved/mismatched project contexts and
constructs a narrow SDK environment with explicit `envFrom`, canonical cwd and
piped stderr. The package-owned Pi session uses the official SDK, drains child
stderr without exposing it, and does not retry ambiguous calls. Every tool
call rechecks the current declaration and ledger before one SDK request; Pi
cancellation, bounded progress and 60-second inactivity/15-minute absolute
timeouts go to the official SDK. MCP text/images map to bounded Pi content;
unsupported binary is explicitly omitted. An approval written during a session
takes effect on its next start.

The offline suite tests the official SDK's real stdio handshake, capability-
gated bounded tools/resources/prompts discovery, operations, progress,
cancellation and cleanup against a generic fixture. It also runs an isolated
real Pi RPC process with `--no-approve` and `--approve`: untrusted files are not
read, absent/stale digests cause zero spawns, and an approved generic fixture
initializes/discovers and exits on shutdown. No provider call is made. PR #77
remains draft; resources/prompts in the model, headless tool-call approval,
agent-comms/ACP projection, Toad controls and independent review are still
outstanding.
