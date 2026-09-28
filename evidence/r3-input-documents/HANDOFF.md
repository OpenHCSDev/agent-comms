# R3 complete input-disposition / ACP cursor ownership

## Candidate and integration

Branch `refactor/input-disposition-documents-20260928`. Implementation checkpoint
`e3715e9`, merged main197 (`15c6a76`) at `4902048`; final commit adds schema-only
codec caching and current fixture/import repairs. Parent owns integration198,
Toad97, installed configured-provider acceptance and activation. No live state,
provider, native bundle or model changes were made by this worker.

## Actual owners and deleted surfaces

| Owner | Current responsibility / migrated callers |
| --- | --- |
| `input_attempt.py` | `InputAttempt` family, `UnknownInput`/`StartedInput`, exact admission/native ID/text/turn transitions, goal review and public projection. No status-switch registry. |
| `input_disposition.py` | `InputDocument` owns snapshot queries and compaction source selection; `DeliveryDocument`/`DeliveryCursor` own alias ambiguity and monotonic boundaries. Both stores inherit A8 `LockedStore` serialization/atomic fsync. |
| `input_drain.py`, `owned_turn.py`, `turn_runner.py` | Typed accepted receipts and current snapshot consumers. The original process-local future queue permit remains necessary; no queue can be reconstructed from UNKNOWN rows. |
| `agent_event_updates.py`, `turn_progress.py` | One document snapshot checks all started keys instead of repeated per-key reads. |
| `goal_management.py`, `goal_actions.py`, `goal_waits.py` | Typed source/review observations; public ACP/CLI result dictionaries retain their existing shapes. |
| `owner_compaction_adaptive.py`, `owner_compaction_commit.py`, `selected_summary_admission.py`, `compaction_journal.py` | Typed original/future input checks and retained input snapshot across native CAS; journal no longer reacquires an input read lock underneath its exclusive lock. |
| `continued_private_session.py`, `supervised_cutover.py` | Exact typed native-binding checks. |
| `transcripts.py:Transcripts.repair_input_routing` | Typed bound-bus input provenance; R1/R6 event parsing is untouched. See method-local patch. |
| `declared_family.py`, `field_codec.py` | `family_discriminator` declares saved `status` for input attempts and default `kind` for other normalized families. R1 `PiPayload.wire_tag` remains the native envelope selector. Codec caches only immutable declaration fields/annotations, never rows or revisions. |

Deleted store `_read`/`_write`, `get`/`status`, store-level public/review/source
facades, duplicated JSON validation/key rosters, and raw cursor boundary/accessor
aliases. Current production and test callers use the actual document/record owner.
No compatibility aliases or second input store were added. Focused tests also
migrate the competing-process fixture to `MessageBus.publisher.publish`.

## Saved data and execution contract

Existing version1 filenames/JSON `status` values, optional notices/reviews and
cursor fields are retained. FieldCodec decodes once and rejects malformed input;
update cannot silently overwrite an unreadable document. Atomic UNKNOWN write and
directory fsync precede acceptance, cursor advance, queueing and native send.
STARTED requires the exact native ID/text/turn tuple and never grants a replay.
Unknown notices remain observations; dismissing one never starts it.

`CompactionJournal.begin(..., inputs=...)` now requires the typed document captured
inside the caller's retained input lock. The production commit caller preserves
its existing outer locks and exclusive input lock through native commit. Journal
entry points which acquire their own snapshot take input shared lock before
SQLite. This removes the observed same-process exclusive-to-shared lock deadlock
without weakening CAS or changing queue policy. The six competing-writer tests
include registry stop/heartbeat/goal, bus, input and send mutations.

## Executed local evidence

- `consumer-final.txt`: 140 passed, ACP input/queue/goals/turn consumers.
- `journal-complete.txt`: 146 passed, documents, admissions, journal, publication,
  continued sessions/cutover and declaration codec. These two receipts precede
  main197 reconciliation; final shared-boundary evidence follows below.
- `native-final.txt`: 2 passed after main197/cache changes, actual ACP plus native
  local fake-provider summary. Original and queued input start once in order;
  foreign ingress variant also passes. No paid provider used.
- `native-cas-repaired.txt`: 6 passed, all competing-writer variants retain CAS.
  Earlier `native-cas.txt` had 23 passed then a deleted fixture API failure; the
  repaired subprocess uses the real publisher owner.
- `codec-final.txt`: 53 passed after main197, typed documents + Pi payload + codec
  + family. Parent independently reports 96 shared-boundary cases passed including
  Pi RPC (`evidence/r3-integration/combined-codec-boundary.log`).
- `codec-schema-cost.txt`: 1,000 record decodes resolve annotations once; no
  decoded document cache. Test also rejects a changed invalid value after caching.
- `nra-working.json`: 79 detectors, zero omissions, complete global context scan,
  zero selected-file findings at the implementation checkpoint. Authored Python
  changes are not claimed as NRA native equivalence proof.

Parent-reported acceptance (not rerun here): exact logical comparison of original
7766 + live14 input rows, 96 original cursors and 2580 UNKNOWN records, plus typed
write/read on owned copies (`evidence/r3-integration/saved-inputs-first.json`).
All four paired Toad pilots passed: current delivery, 932 historical notices,
queue view and native attribution (both routes). No production Toad direct API
change was identified. `TOAD-CONTRACT.md` records the migrated fixture contracts.

## Honest remaining boundary / earlier attempts

Installed actual-provider testing and deployment remain parent-owned. This source
handoff does not claim installed/live R3 acceptance. No currently identified
unfixed product requirement remains. The earlier broad boundary/native attempts
hit 60/165-second budgets; their partial dots are not passes. Earlier migration
failures and lock traceback are retained to explain the repairs, not counted as
current failures or green suites.

Two already-started final fixture receipts will be appended separately when they
finish. `stack-final.txt` is a launcher commitment mismatch: shared live launcher
and source pin differ, refused before input/provider dispatch. A rerun uses the
existing matching integration launcher, without changing either native bundle.
No deployment hold is imposed by these supplementary fixture receipts.

## Parent install procedure

Merge this core branch into integration198 with paired Toad97. Preserve Pascal's
R6 transcript event changes: our only transcript hunk is `repair_input_routing`.
Build the usual isolated candidate and run the prepared parent typed-row queue
acceptance. No explicit live saved-data migration/replay is required: existing
version1 documents decode directly; subsequent mutations use the same saved
format through A8. Parent's normal serial activation handles owner restart.
Rollback uses the preserved prior package; saved files remain readable in the
unchanged external format. Never replay UNKNOWN records as part of rollout.
