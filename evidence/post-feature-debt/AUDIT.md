# Post-feature ownership debt — independent intake

2026-09-28 · Pascal · **audit only; three proposed follow-ups, none started**.

## Answer and source basis

**C0 was in the original plan and its assigned carve/composition/deletion is complete.** Parent checked preserved `files.zip` index lines50/80 and installed R3. Independently, this source's `comms.py:22` has only `Comms.__init__`; operations live on components; `operations.py`/`declarations.py` are absent. The original C0's temporary re-export/mixin staging was superseded by direct ownership and deletion. C0 completion is not a claim that subsequent feature boundaries need no refactoring.

- Own persistent tree: `~/wt/comms-post-feature-debt-audit-20260928`, branch `codex/post-feature-debt-audit-20260928`.
- Inspected source **B**: `9ef2c097efcde0b564fbb0f8cf7fe1a37eaad7b6` = current main198 `ee632a9` (includes main199/R3) plus completed R6 PR200. Paired Toad98 is published; its handoff remains primary. No R6 implementation changed during this audit. Parent reports whole-route copied comparison passes all59465 routes; not rerun here.
- Read-only **R7 overlay**: `~/wt/comms-refactor-r7-selected-execution-20260928`, base `065a4da`, uncommitted implementation observed around05:35–05:40UTC. Read `evidence/r7-selected-execution/SCOPE.md` and actual callers/diffs; this is mutable work, not a final R7 verdict or tested combination.
- Read current dispatch `plans/POST-FEATURE-DEBT.md`, `FINAL-ORIGINAL-PLAN-AUDIT.md`, C0/S7/shared plans and owner overrides. The audit below is an intake for parent to fold into that backlog; it does not replace those plans.
- Full-package-context NRA over seven selected feature files:79 detectors,0 omitted, complete `exact_compact_global`. Three mirror candidates, individually evaluated below. No functional suites, native/provider calls, live reads/writes, migrations, restarts, helper agents or implementation edits. Proposed acceptance below has **not** been executed by this audit.

## Original coverage versus additional work

| Scope | Current disposition |
|---|---|
| C0 operations/declaration split | Complete source; parent verified installed. Actual Comms composition and deleted aggregates, not a file-size inference. |
| S1/A10 events, S2/R1 RPC boundary, S3 nominal states, S4 routing/read basis, S5 registry identities, S6 exports, S8/R4 generation ownership; R2/R3/R5 documents | Existing completed replacements retained in B. Previous acceptance receipts remain authoritative; this audit checks relevant source/callers, not every previous behavioral claim. `owner-presence.json` is a presence/deletion receipt, not a completion certificate. |
| S7 saved entries/R6 | Complete source pair PR200/Toad98; bounded native readers and presentation owners reuse R1. Parent owns integration/route migration/installation. Private proof corroboration below is an additional strict authority boundary, not ordinary saved-history rendering. |
| S3 OPEN-6/S7 transaction plus S5 vocabulary/R7 | **Active Darwin.** SelectedExecution now owns selection, lease, session, TRIAGE/FULL, tool selection, verification/publication and cleanup. WakeAssignment/assignment states replace attention claims; internal generation vocabulary and RecoveryProjection forwarding deletion are in flight. Do not open a duplicate task for these or certify unfinished R7 from this read. |
| D1–D4 compaction | Completed extraction/lifecycle/witness/provider deletion remains recorded; no new phase-based compactor or journal proposed. |

“Additional” below means newly identified residual debt beyond the accepted R1–R6 closures and R7's explicit assignment. It does **not** mean all code first appeared after the original plan: checkpoint and continued-session code already exist at first implementation checkpoint `f60abbb`; normal coding was added later. Each finding identifies its actual feature origin rather than calling all old code new debt.

## PF1 — per-native-tool-call protocol lifecycle (normal channel coding)

**Evidence.** `channel_coding_tools.py:164–245` spreads one call across `announced`, `started`, `finished`, `admitted`, `denied` and a shared event; announce/start/socket-admit/terminal/completeness each reconstruct valid combinations. `selected_tool_broker.py:429+` separately carries `_approved`, `_announced_id/_announced_args`, `_started/_finished` and `completed_call_id` for selected proof. `native_pi.py:853–897` feeds both via the existing OwnerToolSocket contract. Normal coding arrived in `4072ba9` (PR146); later C0/R1 migrated imports/payloads, not this ownership.

**Determining owner and scope.** Extend the existing `OwnerToolSocket` policy boundary with per-call state/behavior owned by the actual `CodingCall` / selected-call transaction. Native execution observation and owner admission are distinct facts: a single simplistic linear enum must not erase the socket/event arrival order. Keep `CodingTool` resource policy, `CodingToolOwner` claims, `NativeToolMode`, peer-authenticated transport and durable selected-slot ledger as their existing authorities. Share genuine protocol behavior through those ancestors; selected one-slot proof and normal multi-call coding stay distinct policies.

**Delete with caller closure.** Replace parallel lifecycle containers/booleans and repeated call-shape/lifecycle decisions in both socket policies; migrate native event callbacks and all socket/native fixtures. Keep opaque native argument payloads where Pi owns the full schema; do not introduce another Pi tool schema, broker, executor, claim store or selected-slot ledger. The owner admission callback, not model JSON, still authorizes mutations.

**Affected acceptance.** Current `test_channel_coding_tools.py`, `test_selected_tool_broker.py`, `test_selected_tool_native_fake.py`, `test_selected_tool_native_package.py`, runner-hook cases, and R7's local native four-tool case. Cover interleaved calls, socket-before-event waiting, duplicate/mismatched announce/start/terminal, failed admission, failed slot fsync, cancellation and lost terminal; no automatic UNKNOWN retry; read/bash/edit/write and release exactly as before. Architectural target: one call owner determines completion, not reconstructing membership across collections.

**R7 seam / disposition.** Proposed independent next surface after R7 caller names settle, or coordinate only its admission-field hunk. Inspected R7 changes selected_tool_broker's `wake_claim_id` to `wake_assignment_id`; no socket lifecycle migration. PF1 is not R7's outer selected execution transaction. Parent assigns; not started.

## PF2 — checkpoint seal and private bus marker ownership

**Evidence.** `private_bus_checkpoint.py:37–125` declares PrefixWitness but separately hand-lists `_witness_record`, `_valid_witness_record`, final/pending seal constructors and final validation. `_saved:180` repeats field/type checks; `_recover_pending_unlocked:380` and verifier:455 redispatch raw pending/final maps. `wire_log.py:102+` and `registry_store.py:120+` independently list the same permissible `bus_meta.json` key sets/version checks. Adding checkpoint fields required changes to both owners. Feature origins: checkpoint `f1ee5d1`; existing-root/default activation `846501f` (PR176). NRA's PrefixWitness mirror is confirmed by these actual writers/readers.

**Determining owner and scope.** `WireLog` owns canonical bus metadata; `PrefixWitness` owns certified-prefix facts and its intentional seal projection. Make final/pending seal data and transition/validation behavior declaration-owned using existing FieldCodec/DeclaredFamily. RegistryStore consumes the same decoded protocol identity instead of its own schema roster. Preserve registry guard identity/rechecks and private file validation: those are separate evidence, not duplicate shape decoding. The seal omits some witness fields deliberately; derive a declared projection rather than dump every field.

**Delete with caller closure.** Retire repeated raw key/type/version rosters, pending/final branch schema reconstruction and witness encode/validate mirrors; migrate install/append/recover/verify/certified-page, WireLog private marker reads/publication, Publisher keyed append, registry guard, NativeSourceCursor/ProvenSourceCoverage and cutover callers. Preserve the existing SQLite certificate/index, canonical append barrier and wire→bus→registry lock ordering; no A8 JSON replacement for this transaction. Actual old saved forms require explicit one-way migration if the schema changes, not coexistence adapters or guessed certification.

**Affected acceptance.** Existing `test_private_bus_checkpoint.py`, private checkpoint cursor integration, source certificate/scale, registry/private writer and supervised-cutover cases. Require copied original-root equivalence, no bus-history rewrite, failed fsync at each pending/final boundary, complete canonical cold recovery, tampered/changed index rejection, absent-audience denial, keyed-response/resource-claim append, warm versus cold agreement and bounded large cursor path. Runtime inode/revision/digest checks must still occur where authority can change; a typed decode is not durable proof.

**R7 seam / disposition.** Proposed queued surface; no PF2 implementation in observed R7. Its native_source_cursor/proven_source_coverage hunks rename assignment/generation callers, so base this follow-up on the completed R7 head. Parent owns live migration. Not a reason to reopen C0 or build a second bus.

## PF3 — strict private native evidence decoding and current consumers

**Evidence.** `native_pi.py:312–452` has two separate saved-header/tracked-user scans in `read_tracked_input_digest` and `_read_native_context_evidence`; the latter also owns a raw seven-key `.input-proof` row schema. `continued_private_session.py:38–87` parses header/role/content/inputDigest again; `coordination_store.py:1467–1495` reloads entries and rediscovers terminal role/content/parent semantics for failure recovery. `historical_native_inputs.py:116` and `native_prompt_binding.py:295` consume those readers. Feature lineage includes stopped native adapter `1a1f8a5` and continued-session coverage `351a493`, later normal coding/R3 callers. B's R6 reader is typed for presentation, while these private security consumers still use raw maps.

**Determining owner and scope.** Extend existing R1 payload/R6 native-entry declarations for actual header/parent/input-digest facts and `NativeContextProof` for strict evidence decoding. Reuse the existing private-file trust boundary and proof reader; do not create another native session store/index or parallel message family. The determining acceptance authority remains **live input/context events plus matching saved proof**, or independently recorded native-start evidence for continued history. Presentation's permissive treatment of unknown entries must not be substituted for strict proof validation.

**Delete with caller closure.** Remove repeated header/user/terminal shape parsing and raw chosen-row mirrors after digest lookup, current native verification, prompt binding, historical inputs, continued-session admission and dead-attempt recovery use typed evidence. `load_native_context_proof:454` only unconditionally raises and is called solely by its test: remove that obsolete public interface/test while retaining negative recovery-authority tests on actual live/evidence consumers. Do not convert “file parsed” into “input committed”, erase UNKNOWN, skip duplicate IDs, or cache observations across source revision changes.

**Affected acceptance.** `test_native_pi.py`, native prompt binding/failure recovery, continued/fresh private session, historical/native-source and selected owner compaction tests. Verify duplicate IDs, wrong session/entry/parent, non-user tracked rows, wrong digest/generation/order, truncated/redirected/insecure files, journal fsync uncertainty and later source mutation all still deny. Actual local RPC should prove the same live input/context receipt; continued-session checks must retain facts and UNKNOWN. No paid provider is needed for this source acceptance.

**R7 seam / disposition.** Queue after R7 because coordination_store/native_prompt_binding/historical_native_inputs caller vocabulary changes there. Observed R7 does not change native_pi/continued_private_session parsing; its modifications in those callers must survive. This is a strict native-evidence follow-up, not an alternative selected execution or a repeat of R6's saved-page renderer work.

## Existing follow-ups and rejected expansions

- Native proof-journal growth remains **existing issue107**. B still bounds `_read_private_file` with `_MAX_JOURNAL = 16 << 20`; this audit neither changes the limit nor claims a retention protocol. Keep issue107's non-hardcoded crash-safe recovery design separate from PF3 shape ownership; they share a future seam, not a second issue/store.
- Canonical-native explicit manual `/compact` remains a **feature/admission design** item. ManualCompaction's documented separate PR95 authority guard is still present. D1's extraction is not thereby unfinished; automatic adaptive compaction is separate.
- NRA's two `TOOLS` mirror candidates are **not accepted new work**. `tools.py:631/750` already derives ChannelSort/ThreadSort choices; the tool parameters differ. The broad DeclaredFamily signal crosses unrelated RPC/CLI/input/content families sharing words such as “fork”/“type”. It supplies no determining-owner proof. Do not replace the tool catalog from this lexical signal.
- Resource claims versus wake assignments, native proof versus human displayed reads, R3 durable input attempts versus process-local queue permits, and native compaction journal versus checkpoint index remain distinct facts. No duplicate store/executor was established merely because they all mention “input”, “claim”, “state” or use SQLite.
- No new assignment arises from module/method line counts. No blanket “three findings” or “zero findings” completion claim. The next backlog has three bounded ownership surfaces plus the two already-known feature/design items; R7 remains its current assigned work.

## Evidence and publication

`owner-presence.json` records relevant actual owners/deleted aggregates. `nra.json` is the initial compact scan; `nra-details.json` expands the three signals for the manual disposition above. Exact successful detailed invocation (about19seconds):

```sh
timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/channel_coding_tools.py src/agent_comms/selected_tool_broker.py src/agent_comms/private_bus_checkpoint.py src/agent_comms/native_pi.py src/agent_comms/native_source_cursor.py src/agent_comms/proven_source_coverage.py src/agent_comms/tools.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload agent --raw-findings
```

Both scans used complete package context, selected reporting files, no cache. No implementation/behavior equivalence proof is claimed. Read-only R7 work was not scanned as if it were already merged. Only audit files are changed in this branch; no shared dispatch edits, preserving parent's backlog ownership. Parent can cherry-pick this documentation commit or fold these entries into `plans/POST-FEATURE-DEBT.md`.
