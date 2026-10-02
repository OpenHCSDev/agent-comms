# Nominal tool request closure

Original S7/A2 closure:29 nominal ToolRequest cases derive names/catalog/parameter
JSON Schema/context binding from their actual declarations. Deleted central TOOLS
and _TOOLS_BY_NAME, ToolDeclaration/ToolParameter, handler-function roster, raw
request decoder and post-decode string/bool/enum recasts. Existing Command,
DeclaredFamily and canonical FieldCodec reused; FieldCodec gains declaration-derived
record schema and constrained-owner choices, no codec subclass. Four exact matching
operations reuse existing CliCommand behavior; GoalAction remains goal authority.
External29 MCP/Pi catalog schemas remain exactly equal to the pre-change capture.

Actual current callers migrate to request declaration lookup. No native_pi, Toad
App or workspace edits; current installation/history/format unchanged. Focused
behavior and installed/native tool/context acceptance in progress on this same PR.
CI deferred. Author-owned AST moves are labeled as authored; no native NRA proof
or zero-debt claim. Parent full-package NRA raw receipt is a structural lead;
manual semantic ownership of this adapter is the refactor authority.

## Final acceptance

- 120 focused source caller/goal/codec/CLI/output checks pass (`final.log`).
- 35 installed-wheel request/goal-input/codec checks also pass
  (`installed-callers.log`).
- New declaration guard adds a tool without editing a catalog/handler/context
  roster, then invokes it against the real filesystem inbox; invalid boolean,
  null and forged family tag rejected. Nullable canonical schema also covered.
- Immutable pinned Pi SDK, loopback HTTP provider, installed noneditable CLI:
  actual `comms_send` and `comms_inbox` tool calls succeed; three provider requests;
  provider receives the real inbox result and `ack=False` leaves message unread.
  No paid call, no mock tool implementation (`native-receipt.json`).
- Installed Toad mounted screen: right-click actual sender row, choose derived
  `comms_stop`, actual selected-root contextual write stops the isolated subject;
  no patched invocation or widget (`menu-receipt.json`). Toad production code
  unchanged; existing public adapter consumer remains usable.
- All 29 complete external MCP/Pi schemas exactly match the before snapshot
  (`schema-receipt.json`), including names, descriptions, defaults and bindings.
- Synced main includes merged native journal decoder and framing owners; no
  edits to native_pi/App/workspace or retained live history.
- Source tools: 1044 -> 867 lines (177 fewer). Canonical FieldCodec adds 64 lines;
  net production source reduction 113 lines. Old central tuple, parameter-kind
  switch, raw request decoder and handler lookup/roster removed, not aliased.
- Initial bounded-output leftover variable and test-only Message.text access
  were caught and corrected before these passing final checks. Universal size
  and benchmark claims remain outside this functional closure.

Parent owns integration/deployment; this receipt proves the isolated installed
paths, not that the live deployment has already changed. CI deferred.
