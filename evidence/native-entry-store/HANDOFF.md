# PR243: combined native history storage handoff

Update: independent full302/604MB current-receiver acceptance is now PASS; see [CAPACITY-ACCEPTANCE.md](CAPACITY-ACCEPTANCE.md). No native source/package change required. The earlier pending capacity paragraph below records the handoff chronology.

Source checkpoint: `2755c2319cfecb7671412d9e9adcdd5504ae526e`, branch `refactor/native-session-entry-store-20260928`. Combines parent PR242 and Pascal PR244 on the requested installed225 base. This handoff replaces earlier checkpoint pending lists; earlier failed receipts are retained honestly.

## Matching candidate

- Native package: `/home/ts/wt/comms-native-session-entry-store-20260928/stack/.pi-native-0d7ebb4f4b5aa1ec/node_modules/@earendil-works/pi-coding-agent`
- Launcher: `/home/ts/wt/comms-native-session-entry-store-20260928/stack/bin/pi-native`
- Python: `/home/ts/wt/comms-native-session-entry-store-20260928/src`
- `canonical-build-acceptance.log`: production preparation, exact patch guards and whole-package verification pass. Earlier candidate packages are superseded. The final package is retained for parent/Lovelace; no live installation, restarts or paid calls performed.

## Implemented and deleted

- Complete EntryStore/SessionContext consumers: SDK, RPC, SessionManager, initial uncompacted chunking, branch summaries, HTML exports, cache notices, prepare/commit/reopen helpers and current tests. Canonical managed compaction retains its existing split-turn admission policy.
- Deleted five superseded writer patch implementations (session-writer-prototype, compaction-journal, writer-coverage, writer-failure-state, compaction-metadata); one complete storage patch owns their locks/CAS/metadata behavior. Removed eager history bodies/maps, old loader/index/rewrite paths, preloaded constructor and old array APIs/types.
- Corrected actual empty-file initialization race, source/destination fork locks, failed-store invalidation/resource cleanup, and cleared branch labels. Store availability owns failure state; no replacement flag on SessionManager.
- Combined proof integration removed attest_input's only yield. Migrated its actual caller to await the coroutine; original input now starts after selected summary/commit instead of becoming UNKNOWN through a Python protocol mismatch.

- Compaction keeps previous summaries before newer source, preserves ordered results despite out-of-order map completion, uses native policy sizing and disk intermediate reduction. Pre-cancelled work invokes no provider; synthesis callbacks use existing progress ownership. No history-sized source/parts/results arrays.
- Proof startup uses indexed tracked-input metadata without historical body decodes. Python `attest_input` is one awaited coroutine, with no compatibility generator/notice restored.

## Caller/deletion map

| Owner | Current consumers | Replaced mechanism deleted |
|---|---|---|
| EntryStore / DiskEntryStore / MemoryEntryStore | SessionManager, proof startup, prepare/reopen helpers | eager fileEntries/byId/labels maps, loaders/index rebuild, preloaded constructor |
| SessionManager writer + store observation/availability | append, reconcile, branch/fork, native commit | five sequential writer patch implementations, `_rewriteFile`, duplicate unusable flag |
| SessionContext | SDK initialization, AgentSession restore/input, RPC count/read | eager initial history restore, old buildSessionContext/settings scans |
| EntryMessageRange / SummarySource | prepare, native chunker, reduction and repeated split-turn compaction | full branch/source/chunk/result arrays |
| EntryBranchRange / persisted entry IDs | branch summaries, interactive cache notices | branch path arrays and message object identity matching |
| Existing output record queue / HTML exporter | normal RPC get_entries/get_messages, HTML/custom tools | eager transport history array and full embedded export serialization |

Compiled current-consumer scan found no calls to removed SessionManager APIs. RPC client's `getEntries` remains the actual external get_entries protocol consumer. Two upstream footer comments mention the removed API; they are documentation, not executable adapters.

## Acceptance

| Receipt | Result and actual boundary |
|---|---|
| canonical-build-acceptance.log | Production native package preparation/verification passes |
| cli-canonical-final.log | Actual canonical CLI compact → writer → fresh CLI reopen passes; 20 loopback requests, original318931 bytes preserved, all60 original messages accessible, one compaction, no provider call on reopen |
| parallel-canonical.log | Actual compiled algorithm: four concurrent maps, ordered multi-level synthesis, cancellation/join, three successive summaries, saved split-turn prior-summary/custom-instruction preservation, configured output reserve |
| selected-owner-corrected.log + selected-owner-remaining.log | All4 previously failing actual ACP real-host ordinary/private summary/decline cases pass after coroutine consumer repair; 1pass14.75s +3pass32.15s |
| writer-coverage-first.log | 18 actual native malformed/race/lock/CAS cases pass, including empty-file initializer and fork source locks |
| writer-failure-final.log | 13 failure scenarios,442 continuation controls pass; failed stores cannot resume writing/reading |
| history-consumers-final.log | 3 branch/export/cache cases pass including cleared label preservation |
| combined-cas-first.log + metadata-cas-current.log | 9pass initially;2 obsolete embedded fixture calls failed, then both repaired cases pass |
| combined-proof.log | Indexed historical/sibling input proof with zero body reads and zero startup emissions |
| selected-summary-current.log | Actual compiled selected summary, including345 model-sized source calls and UNKNOWN/cancel behavior |

All provider responses in these tests are existing local fixtures. They establish protocol/storage/chunk plumbing, not semantic performance of a paid model. Initial failed assembly/fixture/cancellation logs are retained and are not counted as passes. Source chronology and pre-cancel regressions found during the final parallel test were fixed before final package publication.

## Remaining acceptance and limits

Lovelace owns independent **>256MiB full CLI → prepare → journal/inherited-authority commit → strict reopen** acceptance. Exact source/package/launcher were sent on PR243 and PR232. Her earlier302/604MB manager-only memory proofs are not full-chain proof. Parent owns combined source integration, installed/provider acceptance and activation. Latest round2 test receivers must be migrated to the actual matching source contract; no old internal API should be restored.

Native history scanning and chunk sources avoid eager history-body arrays. RPC clients still buffer a requested complete record; explicit tree/custom-render projections allocate requested results. No general bounded-memory claim is made for these paths or arbitrarily large single records. Python historical NativeEntry collections and the noncanonical stock manual helper are separate existing boundaries, not claimed replaced here. Canonical selected compaction retains its existing split-turn admission policy; ordinary native compact supports the tested split-turn history path.

Final package and committed receipts/scripts retained. Owned disposable fixture directories cleaned; no original/live roots modified. Toad T1 settings remains queued after PR243 combined completion, then T3 after T2; no premature Toad implementation.
