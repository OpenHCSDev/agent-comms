# Q6: one field codec, explicit scalar and capture declarations

Deleted the A3 codec selector and the paired Toad SessionCodec/RenderCodec
implementations and callers. FieldCodec now reads an explicit FieldRepresentation
capability from Annotated fields. PathText/TimestampText retain the external text
formats. Renderer UUID/task/result capabilities remain confined to declared
private transport fields; ordinary JSON still rejects those values.

The FieldCodec runtime guard rejects external subclasses even through aliases.
The existing static ownership guard also scans the installed Toad companion when
present. No parallel registry, codec, durable conversion or old-format reader.

Focused installed candidate: 31 passed (scalar new case, strict rejection,
nullable projection/schema and real SQLite row roundtrip; existing A2/A3 guards).
Actual paired Toad wheel: mounted resume modal/PTY and a read-only backup of all
46 actual saved sessions pass; every original row remains unchanged. Actual
private ZMQ renderer/UI: two Toad instances reuse one renderer; Markdown, native
diff, file preview and Read rendering pass. Paired companion receipts live in
Toad evidence/field-representation. Initial missing optional renderer dependencies
are recorded; the complete declared dependencies were then installed.

Core365 was normally merged into this branch after the first focused receipt.
Parent still owns the final paired native/ACP/live gate and installation. This
slice does not claim the intermittent fresh-fork attach failure is resolved.

Patterns: TIME-9/BOUND-1/IMPL-4. A new captured/scalar case adds a capability and
field annotation; it does not edit FieldCodec or a hand-maintained inventory.
AGENTS.md records the latest owner continuous-real-path and deletion requirements.
