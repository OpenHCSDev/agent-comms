# Compaction native evidence lifetime

Base: merged567 `48324afc4d7415afb347cbc1f5f52cfc6820ac73`.
Mendel owns this whole source-read lifecycle; Arendt's566 and Einstein's U2
work are disjoint. Sch owns receiving. Same checkout and installed534 holder;
no new environment, native artifact, provider run or public mutation.

## Source finding and destination

`CompactionBoundary.hold:102` invokes `NativeWitness.retained_task_facts` for
every held scope. That method opens and closes `NativeEvidenceRead` each time.
`NativeEvidenceRead.observe` consequently decodes the whole saved JSONL again
at source capture, currentness checks, reconciliation, commit and admission.
The original configured562 record has a13.562s finish-to-next-preparation gap;
it does not timestamp these reads separately. This is confirmed repeated work,
not a measured attribution of that entire gap or the98.141s provider duration.

The existing reader already owns safe incremental observation: its acquired
`PrivateEvidenceRead` descriptor retains original bytes and hash; every read
rechecks that prefix, checks descriptor/named identity and decodes only the
append. It stores no context proof, input disposition or execution authority.

Extend `OwnerCompactionCommit` with an acquired-operation context, borrowing
that existing reader into `CompactionBoundary`/`NativeWitness`. Migrate all
adaptive, manual and coordinated preparation entrypoints to the same lifetime.
Close the reader on success, refusal, failure and cancellation. Keep all native
writer, registry, input, settings and before/after file fences. In particular,
native evidence observation stays before global bus/registry/input acquisition.
The native commit may append; it cannot replace or rewrite the pinned prefix
unnoticed. One-off read callers can still acquire the same reader for their
short scope through its existing borrow contract; this is resource absence,
not a second source codec or a domain state.

Patterns: BOUND-1 repeated decode and IMPL-13 repeated resource acquisition.
No semantic cache/store, revision counter, lifecycle flag, replay or native
patch. AST before/after includes production, tests and tools; lexical aliases
and dynamic calls are resolved by source reading, not claimed proven by AST.

## Final checks planned for concrete risks

After implementation: one changed-family installed native control covering
source capture, unchanged recapture, actual native commit/append and closure;
original prefix replacement/mutation must refuse. This prevents reuse from
silently weakening source certification. No repeat147s configured provider
journey or unchanged567 context controls. Preserve original m562/m555 sources,
proofs, UNKNOWN and original560 ACP_UNCONFIRMED.
