# Cross-owner channel admission and publication

Receiving owner: Mendel. Original live reproducer: canonical #openhcs seq358,
message c9ef4ce2dcda, wire timestamp1790932784.1737354. Parent owns the one
human send and public recovery; this branch sends no duplicate message.

Trace claim/wake, per-owner preparation, native startup/admission and terminal
publication through existing custody owners. Correct confirmed shared lifetimes
across all callers; preserve original UNKNOWN and stopped-drain dispositions.
Arendt #523 owns selected-summary recovery; Einstein #520 owns optional
compaction policy. Neither claims bus/wake/prompt admission locks.

Source first; existing AST tools map authority/callers. Checks follow the coherent
implementation, and must address demonstrated concurrency or failure risks.
