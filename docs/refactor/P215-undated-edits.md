# P215 — keep undated edit evidence without inventing a float

Status: draft PR #1951, opened with the required Core order identity.
Read-only live unjudged read/encode/presentation passed for all 24 current edits.
Copied rotation reaches a separate missing-checkout defect; acceptance is blocked.

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

## Copied rotation output

Copy: /home/ts/.cache/agent-scratch/pi-cn-phase7/P215-copy-1/bus.
Used upgrade_bus.copy_bus, then read-only SQLite backups into the copy. The
original StorageRule was serialized into the copy before using an isolated HOME,
so HOME did not reinterpret the actual storage paths. Config/state/board writes
were confined to that copy. No native process was started, stopped or replayed.

Actual monitor output, exit 1 (traceback tail):

```text
  File "src/agent_comms/todos.py", line 358, in readings
    base = self.git("merge-base", "HEAD", "origin/HEAD").strip()
  File "src/agent_comms/todos.py", line 353, in git
    raise ValueError(run.stderr.strip() or f"git {args[0]} failed")
ValueError: fatal: cannot change to '/home/ts/wt/pi-plan-claims--P216': No such file or directory
```

pi-cn-phase3 already owns the ProgressSource repair in #1935 and has this actual
copy-path reproducer. No workaround was added here. This is not a successful
monitor run. Its complete original output is rotate.log beside the copied bus.

## Source sanity

Final focused batch: 6 passed in 24.76 seconds. This covers both edit-clock shapes,
strict rejection of a null float, unchanged full hunks/presentation, the external
Core CLI turns endpoint and actual Claude file transport. Two old fixtures omitted
the already-required position; they now supply it. These tests are not claimed as
rotation acceptance. Shared pytest warns about its absent timeout plugin; the
external timeout 300 is the enforced bound. No environment or package was created.

## Structural evidence

Refactor-audit Package parser: 381 Core modules, 280 Toad source modules, zero
parse omissions. No Toad consumer of the changed edit record types was found.
The NRA parser was unavailable (missing tree_sitter); the existing refactor-audit
parser was used instead. Census retained as P215-ast-before.txt in owned scratch.

Before: one EditCall constructor at MutatingPathTool.edit_calls accepted an absent
source clock into a required float; both OrderEditsRead paths returned that row.
After: common EditCallFacts owns capture; the writer returns the explicit record
cases, both readers and UnjudgedEdits declare them, and commander's shared Edit
presentation is composed with each Core record through C3 MRO. FieldCodec's
existing strict record/union decoder chooses the shape; no alternate codec or
new discriminator registry exists. The overlapping alarm consumer remains staged,
not silently claimed complete. Native and external source clocks are not changed.

## Assumed

- Native source history may be read, never replayed or rewritten.
- Old undated history cannot honestly acquire a clock from its read time.
- The original reported exception is real, but its exact old call is not yet
  correlated with the current, subsequently changed Todo assignments.
