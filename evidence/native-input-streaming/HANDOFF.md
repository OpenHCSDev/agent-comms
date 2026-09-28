# Native input proof / transcript boundary

Pascal, `fix/native-input-proof-streaming-20260928`, persistent
`~/wt/comms-native-input-proof-streaming-20260928`, based242 `045bbd7`.
No live writes, provider requests, owner restarts or package installations.
NRA skill/current owner deletion decisions read; no fresh global-scan claim.

## Implemented

- AgentSession startup streams retained `.input-proof` by descriptor, strict UTF8,
  newline and final descriptor/path revision checks. Every row is validated;
  historical rows reserve generation high water and NEVER re-emit acceptance.
- Historical tracked inputs come from Darwin's SessionManager `entryStore`;
  no startup all-message snapshot or parallel input/body Map. Live claims consult
  historical IDs/digests for exact/no-replay and conflicting-request refusal.
  Successful committed inputs leave the live pending map, not durable history.
- Deleted BOTH `patch-native-proof-headroom.py` and
  `patch-native-input-recovery.py`. Current implementation is in the original
  native-input injection; prepare no longer applies either replaced patch.
  Updated only AgentSession source guards in auto-compaction/steering patches.
- Python `_read_private_file` also streamed: deleted16MiB whole-read cap/list.
  Private file ownership/mode, strict JSON/UTF8/complete lines and same opened
  revision remain. Current callers migrated; per-generation duplicate checking
  no longer retains every historical generation in memory.
- Deleted96MiB warning, obsolete capacity-specific diagnostic and quota fixtures.
  Malformed startup remains a real preflight failure with native cause, not a
  guessed size cause or replay permission.
- NativeTranscript tail/reverse now share offset traversal. Large records are
  read once; deleted256KiB silent skip and repeated partial-record concatenation.
  Explicit bounded windows still omit earlier history with existing UI notice.

## Receipts

- `proof-streaming-second.log`: actual patched AgentSession reader,269MB retained
  journal,1,097,728 generations under80MiB V8 heap,peakRSS256,720,896bytes below
  journal268,929,984bytes; zero recovery acceptance, no historical claim copy.
  This boundary test supplies a small store contract, NOT completed native-store
  integration. Failed first import-fixture receipt retained.
- `proof-invalid-current.log`: current generated upstream/auto/steering source,
  malformed/truncated/invalid UTF8/wrong identity/duplicate/regressing generation
  and mid-observation mutation refused.
- `python-boundary-first.log`:77pass8optional skips native evidence/fresh-session/
  transcript cases. `python-capacity.log`:2pass retained >16MiB/old proof generation
  plus concurrent mutation. `transcript.log`:9pass including >256KiB record.
- `live-claims.log`: historical UNKNOWN/no journal remains nonreplayable; live
  fsynced context commits release pending memory, and the next generation still
  binds the historical digest. Two cases pass.
- `deletion-transcript.log`:10pass current deletion guard and transcript boundary.
- `lint-current.log`: current affected Python lint passes.

## Current iterator and native acceptance (2026-09-28)

Merged Darwin243 `63415f8`. AgentSession now uses the actual store's
`trackedMetadata()` and indexed `trackedInputMetadata(inputId)` for startup,
proof-row joins and replay digests. The startup-only body map, all-entry array,
and repeated historical message decoding are gone. Store lookup is indexed
(including duplicate refusal) and metadata carries the determining input digest.

- `metadata-claims.log`:3pass unknown/no-replay, live claim release/subsequent
  context binding, corrupt/truncated/changed journal cases on current source.
- `real-manager-no-bodies.log`: actual AgentSession + actual indexed SessionManager,
  compacted old input and sibling branch input both remain protected, generation73
  recovered, zero live emissions. Store.get was forbidden during this proof/claim
  path: no historical message-body decoding was used.
- `native-cli-first.log`: actual compiled native CLI RPC get_state succeeds with
  journal268,435,686bytes /969,483generations under96MiB V8 heap; no provider/model
  request, all networking forbidden, zero live proof events, journal unchanged.
  Generated journal/session/index files were cleaned after that process exited.
- `assemble-combined-current.log`: removed only Darwin's displaced startup hunk
  (getEntries→entryStore.entries on the deleted trackedEntries Map); every other
  AgentSession context hunk composes without fuzz. Failed predecessor receipts
  remain for traceability and are superseded by these exact current cases.

Own proof/input startup closure is implemented and locally verified. Parent and
Darwin still own whole native package closure/manifest, independent history
consumer/race fixes, metadata/selected summary/native commit framing and quiet
activation. For this source acceptance the candidate used Darwin's current
prepared dist/index.js and core/compaction/index.js exports; Darwin has been
notified to include those in his final generated source patch. No compatibility
exports were restored and no manifest was changed to bless incomplete assembly.

Python NativeEntry.read_evidence still materializes session entries; that is an
existing historical session projection outside AgentSession startup, reported to
Darwin/parent for the full history-store ownership work. This batch also removed
Python proof-journal whole reading, without introducing a second history index.

## Current source accounting

Checkpoint976be19: production source/patches +169/-253; tests including native
JavaScript +156/-236 (before added live-claim regressions). Evidence is separate.
No whole-history cap remains in the native input injection or Python proof
reader; no replaced startup injection is kept alongside the implementation.
