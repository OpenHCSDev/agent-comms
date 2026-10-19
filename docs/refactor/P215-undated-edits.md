# P215 — keep undated edit evidence without inventing a float

Status: implementation checkpoint; application acceptance still pending.

## Source owner

`tool_declaration.EditCall.at` was declared float. The writer in
`native_tools.MutatingPathTool.edit_calls` constructed it from
`ToolStartTranscript.timestamp`, which legitimately includes absence.
`tool_calls.ToolCall.started` constructs that event without a clock;
`ClaudeSession.tool_result` publishes the resulting event pair.

The new common facts type captures the external clock once and returns either
EditCall (required, validated float) or UnrecordedEditCall (no float field).
UnjudgedEdits declares both shapes. Commander's existing Edit presentation is
shared by the two records through multiple inheritance; its boundary decodes
both using FieldCodec. No bad row is omitted; no stored transcript is rewritten.

## Read-only live-bus observation

Read via SQLite URI mode=ro, external_event, thread incarnation
0869d4462913095c0d0d327fa94a1bbf, seq 7472, ordinal 0:

```json
{"kind":"tool_start","position":112524987,"routing":null,"source":null,"timestamp":null,"tool_call_id":"toolu_014GNzrDFAyJzb1e1eBmtZeT","tool_name":"edit","raw_input":{"path":"/home/ts/wt/one-chain/tools/cutover/carry_tool_call_index.py","old_string":"    after = \"carry_hunts\"","new_string":"    after = \"carry_registry_version\"","replace_all":false},"tool":{"tool":"edit","path":"/home/ts/wt/one-chain/tools/cutover/carry_tool_call_index.py","edits":[{"oldText":"    after = \"carry_hunts\"","newText":"    after = \"carry_registry_version\""}]}}
```

This is actual retained undated edit evidence. It is not identified as the exact
historic crashing call: the current Core Todo store now has only three assigned
rows, P215/P216/P217, whose holders are native. No matching currently assigned
external row was found. That limitation must not be silently replaced by a
fabricated assignment or event.

## Shared source and acceptance

PR #1912 currently changes judge.alarm; its dated-age consumer correction is
staged rather than applied to that overlapping function. PR #1929 changes the
ToolStartTranscript class; its return annotation correction is likewise staged.
No codec exception handler, nullable float field, zero timestamp, historical
rewrite or reply filter is added.

The recorded Check runs an infinite monitor then expects exit 0. The narrower
reading is bounded observation of its two-minute progress, not pretending that
an intentional infinite loop normally exits. No live monitor was launched for
this investigation. A safely isolated copy run remains required.

## Assumed

- Native source history may be read, never replayed or rewritten.
- Old undated history cannot honestly acquire a clock from its read time.
- The original reported exception is real, but its exact old call is not yet
  correlated with the current, subsequently changed Todo assignments.
