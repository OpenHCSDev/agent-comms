# S7 canonical source proof and current cursor ownership

Parent assigned after301 main300 closure. Worktree
`~/wt/comms-source-proof-owners-20260928`, branch
`refactor/source-proof-owners-20260928`. Scope claim coordinated on Wegener303:
https://github.com/OpenHCSDev/agent-comms/pull/303#issuecomment-5881471576
No backend/watchdog/native provider edits or live installation. Parent owns install.

## Ownership and deletion

- NativeSourceCursor owns one bus/coordinator/root observation and monotonic write.
- CursorOwner captures thread, participant generation and admission generation;
  it owns the exact live-identity comparison and common immutable native-receipt
  identity check. Capturing the value grants nothing: reads and writes still
  recheck it under canonical wire/bus/registry/SQL boundaries.
- SourceCoverage owns recipient-bound canonical paging, cohort receipt matching,
  historical input evidence, activation floor and bounded pass state. Whole-prefix
  proof remains mandatory; persisted high-water is never used to skip a prefix.
- Existing WakePolicy declarations now own native stage requirements. No second
  policy registry, enum switch or mirrored policy inventory was introduced.
- Deleted the old free read/advance/paging/proof helpers and migrated all callers.
  No compatibility aliases or exports. Deleted the obsolete retained_probe script
  naming the removed D22 staged candidate; retained historical receipts remain.
- Tables, stored formats and external Pi contracts unchanged. UNKNOWN gaps, missing
  receipts, old generations and unbound journals do not become current proof.

## Dependencies / evidence

SourceCoverage reads existing cohort and historical-native owners; policy refers
only to the typed historical evidence under TYPE_CHECKING. CursorOwner consumes
registry/participant/native row identities, and NativeSourceCursor composes both.
No schema bootstrap, input resend, claim acceptance or recovery is added to reads.

Initial installed SQLite/source/cursor/family tests are running. Actual installed
pinned-native/localHTTP selected execution and cursor acceptance follows before
ready. No paid provider or CI wait. Source proof guards enforce removed-call
closure and the S7 function/module bounds.

NRA before scan used complete src/agent_comms context, raw/full payload,1 parser
and1 analysis worker,150s internal/165s wall bound. Two semantic-mirror leads point
to the duplicated live-owner comparison; their proposed DeclaredFamily compaction
members do not own registry identity. The actual captured owner is chosen from
that dependency analysis. CLI has no detector omission inventory in its payload;
manual edits are not claimed NRA-certified rewrites. After receipt follows.
