# Retained facts from the original certified source

Base: bc15a45dabf0c2f0b59633e684bfe8ec799fdab1. Mendel owns this batch.

`HeldCompaction.capture` currently scans and decodes every bus row while holding
wire, bus, registry and input custody. `CompactionSource.require_current` repeats
that scan. Retained-context inspection/export independently uses the same scan.
The current certified checkpoint already holds original sender and addressed
byte pointers; `CertifiedSourceRead.conversation_sources` captures and validates
that membership. No new index, schema, store or retained-fact cache is needed.

Move both consumers to that existing source read. Original
`CommittedDelivery.compaction_messages_for` continues to decide applicability:
addressed messages and authored task declarations, in original sequence order.
Public retained records and context observations remain outside this fact family.
Compaction borrows its already-acquired StoreLock source; it must not reacquire
BUS or weaken owner/input/native/settings currentness. Inspection captures bytes
in its existing wire/registry/input cut and decodes after publication custody.

AST before: 726 production/test/tool modules, no parse omissions. Attribute
resolution is not proved by parsing; the listed owner implementations and callers
were read. Patterns: BOUND-1 (repeated raw decoding), IMPL-13 (reader mechanism at
different scopes), AGENT-2 (migrate both compaction and inspection/export).

This removes unrelated wire decoding. It does not attribute or claim to solve
the original 13.562/13.696 second inter-request gaps or 98.141 second provider
duration. The native child -> separate writer -> fresh child handoff remains a
separate source relationship: current retirement prevents stale in-memory state
after the external write, and inherited descriptor authority remains mandatory.

Final checks must prevent loss/reordering of original addressed/authored facts,
prove unrelated sources are not decoded, and preserve append/replacement and
compaction currentness refusals. Installed retained-context/compaction inspection
on an existing authentic copied source is the affected user path; no provider
reproduction is needed for this read-only batch.
