# Pi MCP client (draft)

The Pi package manifest loads `index.mjs` **by default when the package is installed**.
It connects approved stdio servers when a Pi session starts, discovers tools,
resources and prompts, registers bounded discovered Pi tools (including a
resource/prompt catalog and read/get operations), and closes children on session
shutdown. It provides `/mcp-status`, `/mcp-approve <id>`, `/mcp-deny <id>`, and
`/mcp-allow-calls <id>` / `/mcp-confirm-calls <id>`. Tool calls normally require
a local Pi TUI confirmation. An explicit, separate user-owned call grant permits
headless/RPC calls for the exact project, scope, server and declaration digest;
otherwise an RPC call asks its one controller through Pi's correlated extension UI
with a 15-second deny default. A detached worker without a controller must answer
denial immediately; a direct RPC client that ignores the prompt gets Pi's timeout.

```sh
cd extensions/pi-mcp-client
npm ci --ignore-scripts
npm test
# Install the package in Pi (use your absolute package path):
pi install /absolute/path/to/extensions/pi-mcp-client
# When installed as a local directory, run the package-owned CLI from that directory:
node /absolute/path/to/extensions/pi-mcp-client/bin/pi-mcp.mjs status --json
```

The package-owned `pi-mcp` CLI provides a **static**, redacted JSON status
snapshot (`pi-mcp status --json [--project PATH]`) and a version-2 typed
`pi-mcp inventory --json [--project PATH]` for thin frontends. Inventory has
separate `declarations.user` and `declarations.project` rows, effective-winner
and call-policy fields, and `live.state: "not_running"`; it withholds project
rows before saved Pi trust and never includes executable arguments, literal
environment values or credentials. Neither command opens a transport. They
report saved Pi project trust, not temporary Pi session overrides. `pi-mcp add --scope user|project --id fixture --command /absolute/executable
--arg some-argument [--env-from CHILD=HOST]` creates a native stdio declaration
only after an interactive local TTY acknowledges its ID and digest; `--dry-run`
emits an inert redacted JSON receipt, and `--replace` explicitly replaces a
whole existing declaration. Non-TTY writes are refused. A TTY can be simulated
by another same-user process: this convenience check is **not** human
attestation or an OS security boundary. This action does **not** grant Pi
project trust, project-server approval, or autonomous call permission;
those gates remain independent. Out-of-band `pi-mcp trust approve|deny --id ID
--digest SHA256 [--project PATH]` and `pi-mcp calls allow|ask --id ID
--digest SHA256 [--project PATH]` require a local interactive TTY to show the
full exact declaration and type an action/ID/digest challenge. The trust action
requires saved Pi trust; changed digests are refused before or after display.
Decisions apply to the next Pi turn. A TTY is consent UX, not a security
boundary against an already trusted same-user process. Non-TTY mutation fails;
there is no headless `--yes` approval. Saved Pi project trust (`/trust` in Pi's local
TUI, then restart) is required before the CLI writes project configuration.
Every child uses project cwd, so **both scopes** require a saved Pi trust
decision before MCP reads project declarations or launches: Pi auto-trusts
projects without built-in `.pi` resources and does not recognize `.pi/mcp.json`
as one, while relative user-command arguments could execute project code.
Pi's temporary `--approve` alone is not a saved MCP project trust decision. Project-scope literal `env` is refused
(hidden executable overrides are unsafe); use `envFrom`. User-scope literal
`--env` values are saved in the owner-controlled `mcp.json`.

In an active **native-input-proof Pi RPC turn**, the package emits a bounded
`extension_ui_request` with `method:"setStatus"`, key `pi-mcp/live-v1`, and a
JSON receipt after the native user start. Version 1 includes `source`, the
exact `inputId`, `state:"running"`, `lifetime:"turn"`, and redacted per-server
`id`/`scope`/`state`/`calls`/discovery counts. Agent-comms only forwards a
strictly validated receipt from that same child and input as ACP
`_meta.agentComms.mcpClient`; absent, invalid, stale or disconnected receipts
leave live state **unknown**, not `running` or `unavailable`. The receipt is
informational; consumers must expire it at turn settlement. It **never**
grants trust or calls.
It is not cryptographic provenance against another trusted same-user Pi
extension. The separate static CLI always reports `live:not_running`, never
this active-turn snapshot. Stock Pi without native input proof emits no receipt.

The native config is `{"version":1,"servers":[...]}` with unique server IDs.
Each entry requires `id`, `enabled`, `instructionsPolicy:"status-only"`, and
`transport:{"type":"stdio","command":"...","args":[],"cwd":"project"}`.
Optional user-scope `env` supplies literal values; `envFrom` maps child
variables to host variable names. Project-scope literal `env` is ineligible;
use `envFrom` for owner-controlled values. Unknown fields/transports fail. User config is
`getAgentDir()/mcp.json`; project config is `<cwd>/<CONFIG_DIR_NAME>/mcp.json`.
The project file is not read until explicit saved Pi project trust is active. The package-owned
`getAgentDir()/mcp-trust.json` separately approves the *exact* canonical
project, scope, server ID and complete declaration digest. Changed declarations
need a new approval; project overlays never fall back to user commands. On
POSIX, decision writes durably stage a `.unsafe` marker before rename and
refuse all grants if commit durability becomes uncertain; do not remove this
marker automatically or treat a failed write as a denied approval. Windows
parent-directory durability has not been established: project approvals and
persistent autonomous-call grants are disabled there, and existing ledger
grants are ignored. Windows support is incomplete; this draft is not ready
for a cross-platform release.

Approval shows the complete command, arguments, root and digest in a local Pi
TUI or CLI confirmation without exposing literal environment values. Project
launch decisions and durable call grants remain refused through RPC even when
per-call Pi extension-UI confirmation is available. The
launch-spec builder rejects unapproved/mismatched project contexts and
constructs a narrow SDK environment with explicit `envFrom`, canonical cwd and
piped stderr. The package-owned Pi session uses the official SDK, drains child
stderr without exposing it, and does not retry ambiguous calls. Every tool
call rechecks the current declaration and ledger before one SDK request; a
local `/mcp-deny` revalidates and closes invalid children after its attempt,
including when a decision write becomes uncertain. Out-of-band file edits are noticed on the
next status check/operation or session restart, not proactively watched; close
the session when revoking externally. Pi cancellation, bounded progress and 60-second inactivity/15-minute absolute
timeouts go to the official SDK. MCP text/images map to bounded Pi content;
unsupported binary is explicitly omitted. An approval written during a session
takes effect on its next start.

The offline suite tests the official SDK's real stdio handshake, capability-
gated bounded tools/resources/prompts discovery, operations, progress,
cancellation and cleanup against a generic fixture. It also runs an isolated
real Pi RPC process with `--no-approve` and `--approve`: untrusted files are not
read, absent/stale digests cause zero spawns, and an approved generic fixture
initializes/discovers and exits on shutdown. Offline local-PTY CLI tests cover
exact-digest launch/call decisions, denial and zero server spawn. No provider call is made. PR #77
remains draft. ACP generic extension-UI projection and native-input-correlated
status relay have provider-free fixture coverage; a real Pi→ACP→Toad linked
run, Toad live rendering, HTTP transport, explicit foreign config import, and
final independent exact-byte review remain outstanding.
