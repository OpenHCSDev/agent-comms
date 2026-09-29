# Q8 native commit authority and cold-decline refusal — PR381

## Deleted first; what now owns the behavior

505 production lines replaced/deleted, 559 added: this is an ownership split with
shared mechanics and stricter real admission, not a claim of net line reduction.
OwnerCompactionCommit loses the raw command/metadata dictionaries, duplicate
native commit/reconcile dispatch-and-settle procedure, tuple lock receipt and
repeated native/source comparisons. Registration loses the unused one-shot
attestation API and scalar reconstruction of the held attestation. All source
imports and affected test callers consume the actual declarations; no reexports,
compatibility aliases, second codec, alternate journal or proof index.

- `CompactionBoundary` holds writer → wire → bus → registry → input locks.
  `HeldCompaction` captures their actual source; `CompactionSource` compares that
  complete captured snapshot. Every original write/admission fence remains.
- `NativeRequest` derives commit/reconcile tags from declarations. Summary file,
  usage and cost owners produce the existing metadata digest representation.
  `NativeCompactionWriter` owns one inherited-fd/deadline transport and one
  UNKNOWN/metadata/journal-settlement procedure for both operations.
- Journal records own committed outcome/source decoding; selected identity owns
  exact saved-revision and unchanged input-proof checks. Thread projects its
  own attestation; Registration rechecks it under the existing authority lock.
- Native `ReadyContext` and `CompactionContext` own whether summary decline can
  continue. No Python service guesses this from token counts or nullable flags.

NRA and latest archive-equivalent refactor-audit instructions reread. Applied
IMPL-7 (typed request instead of action bag), IMPL-12/13 (one settlement and one
held authority procedure), IDEN-1 (source/selected identity), IDEN-3 (native
context owns readiness), BOUND-1/TIME-9 (single codec and existing wire boundary).
The packaged ratchet reports **no increasing measures**: class excess -42,
foreign absence probes -6, string subscripts -7; no new chain terms. See
[ratchet](ratchet.txt). The existing S9 guard now includes the new native owner
modules. Real cold-decline regressions mechanically protect refusal behavior.

## Actual installed failure and fix

Parent's four failed decline continuations used a fixture with 9503 selected
context tokens, model window10000 and reserve1000. Normal ACP preparation
cold-reopened it: native d396 correctly restored CompactionContext above its
9000-token input budget. The fixture forced an unsupported summary by removing
available summary models. A returned journal decline then permitted binding the
original, although native beforeInput still refused. PrivateInputs/send_fence
was not the failing barrier.

The valid soft-decline fixture now uses the actual SettingsManager: admit the
saved context under its original reserve, then increase reserve via
applyOverrides on that ready manager to request adaptive compaction. All four
ordinary/private + queued future cases pass on **unchanged d396** (35.14s).
A separate cold-overbudget ACP case remains RED on d396, preserved in
[d396-cold-red.txt](d396-cold-red.txt).

Production correction: every selected summary decline consults the existing
native context owner. ReadyContext preserves its reason; CompactionContext
returns `context_requires_compaction`. The existing strict refused-summary
lifecycle handles it before a returned original grant or input binding. Original
saved bytes stay identical, provider request list remains empty, original input
stays unbound. No budget increase, forced compaction, journal clearing or replay.
The native receipt shape, source witness, proof revision and index remain unchanged.
Wegener378 owns the indexed proof implementation; direct PR coordination recorded.
Reviewed378 head7070ad1f: it changes the contents/reader of `.input-proof`, not
NativeWitness, SessionRevision, selected source identity or helper outcome shape.
Our unchanged input-proof stat fence still applies; only the native manifest
requires combined regeneration when the parent pairs the two changes.

## Evidence and limits

All Python/native receipts below ran from this checkout's **noneditable installed
wheel**. Controlled provider transport is loopback-only. No live default, launcher,
user root, real user input or shared package was modified.

- [33 registry/held-authority tests](authority-rerun.txt), including child-held
  locks after parent death, pass.
- [7 actual d396 native tests](native-first.txt) pass: commit, metadata corruption
  and rehash refusal, lost-result exact reconciliation, stopped/changed owner.
- [12 continuous installed ACP/native cases](corrected-native-journey-rerun.txt)
  pass109.70s: committed publication-before-bind and clean decline, ordinary/private,
  future queue and next retained-history turn; postcommit correction refusal;
  disconnected UNKNOWN no replay; cold refusals before bind with zero provider
  calls and exact history unchanged.
- [13 canonical-package authority cases](native-authority-closure.txt) pass33.42s:
  competing writers, source changes, unsettled UNKNOWN, inherited authority,
  parent death, intent and outcome fsync failures/exact reconciliation.
- [31 boundary/package/S9 guards](focused-packing.txt) pass. All14 input-drain
  cases passed in [caller-closure-final.txt](caller-closure-final.txt); that run
  then found an obsolete recovery fixture argument, migrated in place.
  [3 actual recovery cases](recovery-callers.txt) subsequently pass5.76s.
- The final exact unattempted-original caller receipt is [original-caller.txt](original-caller.txt).
- Ruff F/E9/I and git diff --check pass. CI deferred, no optional broad matrix.

Failed receipts are retained: initial scratch parent missing, first copied
package root mode0755 rejected by the unchanged trust guard, and remaining
obsolete test references (`bridge.root`, old ACP metadata, pre-state-migration
UNKNOWN names/optional native_id, removed real_host argument). Tests migrate to
actual current owners; uncertain test rows use real durable binding, not renamed
assertions hiding an UNKNOWN transition.

## Complete package / integration

Canonical `stack/bin/prepare-pi-native` **passes**, complete tree:
`0281bb9795f02e3549062bffb68933c6518fc99f0e68c8a9daff5f7903a94f64`.
Ready package, preserve until parent copies it outside this worktree:

`/home/ts/wt/comms-q8-native-commit-20260929/stack/.pi-native-151d0ffb1901091e/node_modules/@earendil-works/pi-coding-agent`

The first copied candidate differed from canonical generation solely in the
nonexecuted GNU patch backup `dist/modes/rpc/rpc-mode.js.orig`; see
[canonical-diff.json](canonical-diff.json). The pin now covers the exact canonical
package including that existing artifact. No trust check was relaxed. Runtime
files for the12-case native receipt and canonical final artifact are identical;
13 authority gates execute the final canonical artifact directly.

Parent owns merge, paired install, affected live check and activation. If378 is
integrated simultaneously, fold these two native context/RPC changes into its
complete package; do not overwrite its proof index. This completes assigned Q8
bridge behavior, not every remaining history/event/T4 plan surface.

Owned disposable roots and env: `.scratch`, diagnostic `stack/.pi-native.stage.*`,
`.venv`. Remove after active process references finish; retain this receipt and
canonical package for parent integration. No predecessor or live root is cleanup
scope.
