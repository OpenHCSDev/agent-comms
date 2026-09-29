# Issue107 / PR378 FINAL — ready for parent source review and paired cutover

## Deletion and authority

Removed55 old Python proof-reader lines,108 native patch lines and the125-line prototype-only streaming test (diff against main1832c930). The full proof-row startup scan, Python historical proof scan and native generation mirror are gone. One indexed transactional `.input-proof` journal owns proof; NativeContextJournal generates native SQL from the existing TypedTable fields. Native session JSONL remains the durable claim/UNKNOWN source. No lifetime cap, compatibility reader, replay, extra proof cache or paid provider call.

Patterns: BOUND-1/BOUND-2 typed decode and source corroboration; MEMB-5/AGENT-8 declaration-derived native schema/queries guarded against drift; TIME-1/TIME-2 current-only runtime with a temporary outside-src converter. Final core ratchet against1832c930: no increases, one fewer long boolean chain, six fewer terms, no foreign-absence or codec-subclass increase.

## Exact installed evidence

Production source c51f9af4; later final checkpoint changes only tests/evidence. Immutable matched package:
`stack/.pi-native-fcadec01235eb0b5/node_modules/@earendil-works/pi-coding-agent`
Manifest: `fcadec01235eb0b57417de418364681950311c086c1f06920151469e2728e40f`.
Current noneditable candidate: `.artifacts/native-proof-checkpoint/runtime`.
All loaded core modules were checked under that installation; source imports were not substituted. Package verification passes.

- **16 distinct actual installed native cases passed**:11 in the first run,4 in the missing-ACP/corrupt-proof followup,1 attached compaction case. Logs: installed-native-first.log, installed-native-remaining.log, installed-native-compaction-installed.log; corresponding installed-imports receipts.
- First run honestly had11PASS/1FAIL96.13s: the ACP case lacked the optional ACP dependency and never started. Installed that dependency; the missing ACP path plus actual malformed/torn/source-lineage refusals passed4/4 in36.78s. No unchanged11-case repetition.
- Retained-child/attached-ACP manual compaction -> child retirement -> fresh saved reopen -> one new explicit input **PASS21.98s**, four local HTTP requests. Initial invocation SKIPPED because AC_NATIVE_STACK_BIN was absent; second failed before input because it named the source launcher from the installed wheel. Correct invocation names that installation's pi-comms-native. Both failed/setup receipts retained; only final actual result counted.
- Installed proof growth6,504,448 /39,059,456 /136,839,168bytes: cold reopen/new-turn3.258/3.170/3.196s; native peakRSS168,636,416 /167,055,360 /168,222,720bytes. Exactly four fresh inputs and local HTTP requests; accepted generation1 still corroborates. This is real saved native state with synthetic repeated valid proof generations, NOT a137MB native transcript. The separate native JSONL metadata-index rebuild still scales with transcript size and is not claimed solved by107.
- Five actual SIGKILL transaction boundaries and three initial-schema publication boundaries passed. Hot rollback works through native reopen and query-only Python history reads. No recovery emits a live receipt or automatically resumes UNKNOWN work.
- Real native accepted+UNKNOWN history -> one-shot old-proof conversion -> duplicate/conflicting-ID probes -> new explicit input passed; native bytes, historical proof and UNKNOWN disposition retained. Malformed/torn/foreign-current-source journals refuse actual native preparation before any input/provider.
- Selected four-tool publication and actual ACP saved-load/new-input both pass.
- Final conversion interruption/invalid-prior/schema-generation/deleted-path guards: **9PASS0.89s**. Converter is outside src and must be deleted after reviewed quiet activation; runtime never reads the retired format.
- JavaScript native SDK/restart callers migrated to the generated current schema: **47PASS4.06s**, no provider use. Their initial39PASS/8FAIL receipt is retained: two removed getEntries calls, five obsolete live-map assertions after durable claim transfer, and a helper hardcoded to /var/tmp. Assertions now inspect actual durable native entries and still require exact dedup/no extra provider. Three JS fsync-mocking cases were deleted in favor of installed OS-level SIGKILL/publication/dedup coverage; no obsolete fs implementation restored. Opt-in paid real-rpc test reader migrated but NOT executed.

## Known unrelated failure — assigned, unchanged

`test_selected_original_survives_auxiliary_cursor_over_100_initials`:covered_seq103 vs expected102 also fails on unchanged mainbeaa8c94. Exact baseline and parent/Dalton handoff are in PR378 comment5887839991 and cursor-main-baseline.log. No assertion weakened or permanent skip added. Other affected caller corrections/results retained below.

## Parent action and limits

PR is ready for review/landing; **NOT LIVE**. Parent owns independent source review, quiet conversion, paired activation, final normal App/painted-live saved-history acceptance. Follow stack/native-proof-cutover.md, preserve all original native history/UNKNOWN inputs, convert proof only under stopped writer ownership. No state reset/replay. Old backup rollback is allowed only BEFORE any new native write; otherwise repair forward.

Preserve fcadec until parent verifies its canonical copy. Owned completed test installs/source exports, old86c2/22f packages and test copies removed after process-reference checks: approximately624MB logical total (physical reclaimed space may differ). Current candidate source, wheel/runtime and evidence retained. No live/global package or original user history touched.

---

# Issue107 / PR378 current checkpoint — installed final paths in progress

Source c51f9af4 (normal merge of main1832c930); substantive proof commit24aa16fb.
Matched immutable native candidate: `stack/.pi-native-fcadec01235eb0b5/node_modules/@earendil-works/pi-coding-agent`.
No live/global package mutation, user history change, paid provider or replay.
Parent owns paired install and final normal App/painted-live acceptance.

## Completed evidence

- Actual noneditable installed native:11PASS,1ACP setup failure96.13s. The ACP case did not start because candidate omitted optional agent-client-protocol; installed that dependency and rerunning only the missing ACP path plus required actual malformed/torn/source-lineage refusals. First failed receipt retained; no unchanged11case rerun.
- Installed growth:6.5/39/137MB proof history, cold new-turn3.26/3.17/3.20s, native peakRSS168.6/167.1/168.2MB. Real saved source + synthetic repeated context generations, actual CLI reopen/new request. All actual core imports verified under the wheel, no PYTHONPATH.
- Installed OS-level SIGKILL:5existing-journal +3initial-schema boundaries pass. SQLite rolls back only the interrupted context; first publication cannot expose an empty schema. Query-only Python history reads also recover a hot transaction without needing a model turn. No recovered acceptance is emitted.
- Installed actual accepted+UNKNOWN saved inputs -> old proof representation -> one-shot conversion -> exact and conflicting duplicate-ID probes -> genuinely new input PASS. Original native history and accepted generation1 remain intact. UNKNOWN receives no synthetic receipt or automatic replay.
- Installed selected FULL four-tool/publication case passes. Actual ACP saved-load/new-input case is the remaining installed gate.
- One-shot converter interruption/corruption/declaration guard:8PASS1.53s. Interrupted operator conversion retains its writer fence, never silently steals it. No runtime old-format reader.
- Affected current-format callers: source71PASS11skipped before a stale367 bind monkeypatch; corrected it. Remaining97PASS1skip plus two stale367 child class references and test socket-path-length failures; corrected those and affected7PASS6.40s. No compatibility exports restored. First combined caller run was ABORTED after common fixture-family decode errors (72failed55passed11skipped), not green; corrected fixtures/converter to construct the current declaration.
- One separate pre-existing cursor test fails identically on unchanged mainbeaa8c94:covered_seq103 versus102. Exact baseline4.50s receipt and Dalton ownership handoff posted on PR378. Assertion unchanged.
- Source debt ratchet has no increase; long boolean chains-1, terms-6 in native_pi.py. Rerun against integrated parent main before readiness.
- Removed completed test installs, old native candidates86c2/22f, baseline source export and old proof test copies after process-reference checks (~480MB logical total including earlier cleanup). Preserve fcadec until parent copies/verifies it.

## Ownership and deletion

BOUND-1/BOUND-2: proof decoding, current-source corroboration and SQL recovery stay on NativeContextJournal, not consumer-shaped copies. MEMB-5/AGENT-8: existing TypedTable field declarations generate all native schema/column SQL; a schema-drift test guards the generation. TIME-1/TIME-2: old production JSONL proof scanner and native full-history row/generation mirror removed; prototype-only streaming test deleted in favor of actual native saved-state/crash journeys. Temporary one-shot conversion lives outside src and must be deleted after reviewed cutover. No new codec subclass, registry, cap, live-package mutation or provider call.

The native JSONL entry-index rebuild is a separate existing startup cost. Proof history is indexed; this does NOT claim total native-history startup is independent of the number/size of native session entries. Malformed/historically corrupt sources refuse; they are not erased or reclassified.

## Remaining

Finish installed ACP + actual native malformed/torn/foreign-source checks; update final receipt/ratchet and mark ready for parent source review and quiet paired cutover. Deployment/rollback contract is stack/native-proof-cutover.md. After any new native write, rollback by restoring old proof is forbidden; repair forward.

---
## Earlier checkpoints (superseded where noted above)

# Issue107 active implementation — NOT READY

Wegener sole native/proof scope, branch fix/native-proof-checkpoint-recovery-20260929 in persistent WT. Parent owns live; no live package changes.367 merged10eb0ce6, awaiting parent paired installation;368/372 merged; issue107 follows after its complete handoff.

Current d396 has no total proof-size cap, but AgentSession._loadNativeInputState revalidates every historical .input-proof JSONL row on each constructor. stack/test-native-proof-streaming.mjs constructs Object.create(AgentSession.prototype) and proves streaming256MiB, not actual saved-native/RPC crash recovery. NativeContextProof Python corroboration also reads all proof rows. Need actual native cold startup/accepted-ID/UNKNOWN/cross-commit proof on increasing history, not cap bump/prototype alone.

Implementation started: existing NativeContextJournal now derives TypedTable (same NativeContextRecord fields carry SQL metadata). Owns generated schema/index/append-only lineage constraints and native_contract; tools/render_native_proof_schema.py derives stack/native-proof-schema.mjs. stack/native-proof-journal.mjs owns SQLite indexed append transaction/recover at SAME .input-proof path, DELETE journal with EXTRA sync and parent fsync before receipts. No recovery acceptance emission; native session retains original accepted/UNKNOWN claim authority. No runtime JSONL fallback; needs explicit offline durable conversion preserving all existing records. Node module syntax and Python declaration/DDL construction passed only; NOT native acceptance. New module not wired yet. Preserve work and continue full caller/reader/writer/package closure before claiming ready.

Immediate work: integrate new journal into base native patch, delete _nativeProofRows/_loadNativeInputState proof full scan and duplicate generation counter; Python exact evidence reads indexed typed rows; offline atomic conversion with prior journal lineage validation; current source test fixtures read/write new format, delete obsolete prototype/JSONL guard expectations. Need generated SQL drift guard; real native input->saved->reopen->new input with >128MiB history; SIGKILL/failure at schema/transaction/commit/parent-fsync publication boundaries, torn/malformed/current-source/mismatched-owner/UNKNOWN claims preserved. Build NEW immutable native package/manifest using current stack workflow, no existing package mutation, preserve failed receipts.

Design risk to keep explicit: native SessionManager DiskEntryStore currently rebuilds an ephemeral unlinked SQLite metadata index from the entire native session on open; _loadNativeInputState also iterates every tracked native input. Proof-journal bounded recovery does NOT prove total native-history startup independent of history size. Keep measurement separated; move tracked validation to existing EntryStore decode owner only with complete duplicate/format checks. Do not claim constant total startup from fixed-native-history proof-growth tests. Evaluate any persistent metadata change with existing owner and preserve mutation/lineage authority.

Resource check warning /home10.8GiB /6.4GiB swap9GiB; no new agents/paid provider/big parallel runs. Removed completed q4-installed/q4-current derivatives after process refs; cleanup.json. Keep367 candidate, owner-startup candidate, source/evidence/globald396. Before large-history tests recheck headroom and bounded serial scratch.

## Current checkpoint (draft, not deployment-ready)

Native `_nativeProofRows` and its full-history startup validation/generation mirror are deleted. AgentSession uses NativeProofJournal at the same proof path; Python corroboration queries the existing NativeContextJournal/TypedTable primary key. Current context lineage is checked against SessionManager on reopen; historical input claims/UNKNOWN remain in native history, no recovered acceptance is emitted. Current-only SQLite, declaration-generated schema, no runtime JSONL reader. Parent main beaa8c94 merged normally.

NEW immutable candidate: `stack/.pi-native-86c2f983aa2ecb0c/node_modules/@earendil-works/pi-coding-agent`; manifest matches full package. `build-first.log` failed because old preparation input pins changed; corrected all affected preparation pins. `build-second.log` completed canonical preparation steps with zero fuzz and verified tree. Global/live d396 untouched.

Actual saved native growth journey `native-growth-first.log`: 1 PASS18.56s, real pinned CLI + normal TurnSession and local HTTP. Seeded one real input, then synthesized repeated valid context records against that exact saved native source to grow proof history. At 6,504,448 /39,059,456 /136,839,168 bytes, actual cold reopen+new input was3.23/3.23/3.31s; native peakRSS174,096,384 /170,672,128 /175,538,176. Exactly4new inputs/4loopback requests overall; historical generation1 still corroborates. These timings include native/package/turn overhead, not just indexed SQLite recovery. They do not establish constant cost for rebuilding the separate native JSONL entry index.

One-shot quiet-runtime converter implemented in tools/cutover (NOT RUN on live): same SessionManager exclusive writer fence, exact prior proof validation and source corroboration, generated transactional schema, independent fsynced backup, atomic replacement, no native history rewrite/UNKNOWN reclassification. Delete tool after parent-reviewed cutover. Crash tests must settle the first-file schema publication boundary too; existing truncated/malformed databases must fail closed.

Remaining before ready: actual killed-writer publication/recovery boundaries and UNKNOWN/replay refusal, converter tests including corruption/history preservation, current-format caller/fixture closure, installed saved native/ACP acceptance, ratchet/review and deployment/rollback instructions. No paid provider, no CI hold, no live changes. Owned merged derivative cleanup removed45MB (cleanup-merged-derivatives.json); current test root disposable after process check, source/evidence retained.
