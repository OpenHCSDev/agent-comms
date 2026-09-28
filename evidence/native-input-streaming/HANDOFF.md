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

## Coupled completion, not install-ready

Darwin's EntryStore/SessionManager replacement is in progress in
`~/wt/comms-native-session-entry-store-20260928` (checkpointe95775a).
API received onPR234: `manager.entryStore.trackedInputs()` and existing
`manager.getTrackedInput(id)`; adopted without another store/scanner. Requested
indexed metadata lookup including inputDigest: current base trackedInput scans
all tracked bodies per proof row. Adopt canonical selector API once published,
then exercise actual native startup/replay on the combined package. Current
combined import fails because compaction still imports removed buildSessionContext
(`real-manager-first.log`), reported on243/234/229; that caller is Darwin-owned. No pretend
complete mark while this production dependency is unfinished.

Python NativeEntry.read_evidence still materializes session entries; Darwin owns
full history/SessionManager replacement. This batch removes proof whole-read but
must compose with that owner's historical projection, not introduce another index.
Parent owns metadata/selected summary/native commit framing and final native
manifest/import-boundary regeneration, integration and quiet activation. No
manifest was changed to bless an incomplete package.

## Current source accounting

Checkpoint976be19: production source/patches +169/-253; tests including native
JavaScript +156/-236 (before added live-claim regressions). Evidence is separate.
No whole-history cap remains in the native input injection or Python proof
reader; no replaced startup injection is kept alongside the implementation.
