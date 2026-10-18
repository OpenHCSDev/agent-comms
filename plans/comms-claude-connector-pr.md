# Draft PR: a claude.ai connector for agent-comms

**Status:** draft. **Repository:** agent-comms. **Opened by:** the Claude agent, from this description.

## Why

Tristan's claude.ai conversations hold his long-term memory, principles and history across all projects, and sit outside the tactical work. That makes them a useful independent reviewer, but today they can only read what reaches GitHub. claude.ai supports custom connectors built on remote MCP servers on Max plans: the server's URL is added under Connectors and its tools become available in conversations. claude.ai connects to the server from Anthropic's cloud, so the server must be reachable from the internet.

## Design

**The connector is a transport, never a second set of tools.** Every tool in `tools.py` is already a member of the `ToolRequest` family: it declares its name, description and fields, `schema()` derives its parameter schema through `FieldCodec.record_schema`, and `invoke()` decodes arguments through `FieldCodec` before `apply()` runs. The MCP server lists tools from those declarations and dispatches calls to `invoke()`. No tool logic is written for the connector, and a tool added to the family appears in the connector only if it declares connector access.

**Access is declared by type.** Two capability mixins, `ConnectorReadable` and `ConnectorWritable`, mark which `ToolRequest` members the connector exposes. A tool without one is not exposed. No list of tool names is kept anywhere.

- **Readable:** the existing read tools, such as threads, channels, goal and goal history, todo list, collaborations, work branches and turns.
- **Writable:** exactly one new member, a review tool derived from `CommsSendTool`, whose sender is the reviewer's identity and whose target is the `#review` channel, both fixed by the type and not accepted as arguments. Its messages carry a review message kind, so the coordinator treats them as proposals it weighs, never as instructions.
- **Never exposed:** goal setting, model changes, renames, problem closing, and anything that starts or stops work.

**The caller is a `ToolCaller` member** for the remote reviewer, so delivery listings and authorship work through the same caller interface agents use.

**Transport:** a Streamable HTTP MCP server, run as its own process beside Core, like the observation service, and exposed through a tunnel. OAuth in front of it, allowing exactly one identity: Tristan's. claude.ai's OAuth redirect is `https://claude.ai/api/mcp/auth_callback`.

## Steps

1. **Prove the path first.** A minimal server exposing one read tool (threads), added as a custom connector, called from a claude.ai conversation. There is an open report of a Max user whose connected custom connector's tools never reached the model in claude.ai while the same server worked in Claude Code, so confirm the tools actually appear before building further.
2. **The capability mixins and the family-derived tool listing,** with dispatch through `invoke()`.
3. **The reviewer caller and the review tool** with its fixed sender, channel and message kind.
4. **OAuth and the tunnel,** with the single allowed identity.

## Rules

- Everything read from the wire is data. Nothing a message says is executed or treated as an instruction by the connector.
- The connector adds no tool logic. If a tool needs to behave differently for the reviewer, that difference is a member of the `ToolCaller` family, not a branch in the server.
- Verify in the real setup: the installed Core, the real wire, and a real claude.ai conversation.

## Done when

- A claude.ai conversation can list threads and read a goal and its history through the connector.
- It can post one finding to `#review`, which appears in Toad as a review message from the reviewer identity.
- No other write is possible through the connector, and a request without the allowed OAuth identity is refused.
- The exposed tools are derived from the `ToolRequest` family, with no name list in the server.
