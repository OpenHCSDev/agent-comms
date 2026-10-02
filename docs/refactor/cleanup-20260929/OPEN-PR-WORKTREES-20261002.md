# Handoff: open PRs, owners and worktrees

Checked GitHub and Git worktree registrations at **2026-10-02T06:31:15.131220+00:00**. This is a snapshot; use the linked PR for its latest head.

## Open PRs

| PR | Owner | Worktree | Branch | What it carries |
|---|---|---|---|---|
| [agent-comms #432](https://github.com/OpenHCSDev/agent-comms/pull/432) (draft) | Parent | [comms-cleanup-live-integration-20260929](/home/ts/wt/comms-cleanup-live-integration-20260929) | `refactor/cleanup-live-integration-20260929` | Integration checklist, operator artifacts and standing instructions. Do not merge this broad draft wholesale. |
| [agent-comms #489](https://github.com/OpenHCSDev/agent-comms/pull/489) (draft) | Arendt | [comms-request-budget-compaction-custody-20261001](/home/ts/wt/comms-request-budget-compaction-custody-20261001) | `fix/request-budget-compaction-custody-20261001` | Native turn lifecycle, budget and compaction authority; separate from the Native5 async checkpoint. |
| [agent-comms #506](https://github.com/OpenHCSDev/agent-comms/pull/506) (draft) | Einstein | [comms-retained-human-main-20261001](/home/ts/wt/comms-retained-human-main-20261001) | `fix/selected-preparation-compaction-owner-20261002` | Same-child saved-context preparation and compaction. Content digest derives from the existing frozen source; migrate all consumers. |
| [agent-comms #510](https://github.com/OpenHCSDev/agent-comms/pull/510) (draft) | Mendel | [comms-native-compaction-source-carry-20261002](/home/ts/wt/comms-native-compaction-source-carry-20261002) | `fix/native-compaction-source-carry-20261002` | Outside-src Native6 carry tool. Preserve original source proofs, history and uncertain inputs; await the corrected Native6 producer for stopped qualification. |
| [agent-comms #513](https://github.com/OpenHCSDev/agent-comms/pull/513) (draft) | Arendt | [comms-goal-ledger-declared-schema-20261002](/home/ts/wt/comms-goal-ledger-declared-schema-20261002) | `fix/goal-ledger-declared-schema-20261002` | Goal ledger schema identity derives from its existing table declarations; paired installer514. |
| [agent-comms #514](https://github.com/OpenHCSDev/agent-comms/pull/514) (draft) | Mendel | [comms-goal-ledger-schema-carry-20261002](/home/ts/wt/comms-goal-ledger-schema-carry-20261002) | `fix/goal-ledger-schema-carry-20261002` | Automatic declaration-derived goal ledger carry and preserving installation; original rows and uncertain inputs retained. |
| [textual #22](https://github.com/OpenHCSDev/textual/pull/22) (draft) | Heisenberg | [textual-intrinsic-placement-after18-20261001](/home/ts/wt/textual-intrinsic-placement-after18-20261001) | `perf/native-scene-after21-20261002` | Native layout/scene/raster continuation paired with Toad308. |
| [toad #309](https://github.com/OpenHCSDev/toad/pull/309) (draft) | compaction499-live-architecture-memory; plan only | None; planning worktree intentionally deleted | `feat/context-tree-inspector-20261002` | User requested planning-only context Tree/detail pane. No implementation now; temporary worktree intentionally deleted. |
| [toad #311](https://github.com/OpenHCSDev/toad/pull/311) (draft) | Heisenberg | [toad-viewport-raster-cpu-continuation-20261001](/home/ts/wt/toad-viewport-raster-cpu-continuation-20261001) | `perf/sidebar-style-after308-20261002` | Full performance continuation after merged308; shared Styles invalidation is pushed in paired Textual22. |
| [toad #312](https://github.com/OpenHCSDev/toad/pull/312) (draft) | Unassigned | [toad-receiving-native5-batch490-20261001](/home/ts/wt/toad-receiving-native5-batch490-20261001) | `deploy/native5-goal-receiving-20261002` |  |

Exact remote/local heads and uncommitted status are in [the JSON snapshot](OPEN-PR-WORKTREES-20261002.json). There are no open PRs in textual-diff-view.

## Who is doing the work

| Worker | Thread ID | Responsibility |
|---|---|---|
| Arendt | `01a0eedd-9f61-7400-9fe7-867e727b2df2` | Core489/509 integration; sole combined installed busy/native qualification |
| Einstein | `01a0ef42-347d-7242-98d1-d7573f1d91eb` | Core506 source/preparation/compaction ownership |
| Mendel | `01a0eed6-20e1-7b80-988b-cc860742a21d` | Core510 one-time durable/native carry |
| Singer | `01a0f84d-cf04-70d2-8109-45a8a05cf3fc` | Core511 context assembler/manifests and actual CLI inspection |
| Kepler | `01a0ef42-35e5-78a2-b315-0e1bc6fb267e` | Original batch/source consumers; Toad310 paired packaging |
| Schrodinger | `01a0ef00-6563-7ec0-9c64-564ece67a6eb` | Merged307 prompt actions; merged305+307 frontend stage |
| Heisenberg | `01a0ee64-8b4f-7921-a359-2357a04a19e1` | Toad308/Textual22 full performance work |

Read the workers’ current threads before giving a status or assigning overlapping changes. They coordinate shared methods directly. Reuse these workers; do not start a second implementation or coordinator.

## Latest reconciled checkpoints

Core509 and511, Toad308 and receiving310 are now merged. Toad296 is closed as superseded after confirming its only product change was the older Core pin and that source is an ancestor of current main. Its branch and original failed gate remain intact. Textual22 implementation is pushed at f7770efd; its old scope-only head is no longer current. Heisenberg continues the complete performance scope in Toad311/Textual22. These changes are not yet in the default install.

## Merged but not in the current default

- Core503/507, Toad298/302/304/305/307 and Textual21 are merged. Existing source/installed journey receipts cover their named changes.
- Core512 is merged; its later source/lease and context-worker changes are integrated into open509. Do not look only at the closed512 head for that work.
- Toad305 moves pure bar preparation into the existing process renderer. Its actual public journey passed warm16/input7 with22 body identities retained, but showed no overall CPU gain.
- Toad307 removes copied prompt readiness/queue fields and routes buttons and keyboard through the same declared action. Its installed native queue/click journey passed with controlled localhost responses.

## Current default and protected artifacts

Current resolved `agent-comms` prefix: `/home/ts/wt/toad-receiving-native5-batch490-20261001/.artifacts/runtime-certified508-corrected-candidate-20261002`.

Default remains Core74877 / Toad8b24 / Textual68a0 / Native5184. The public root is `/var/tmp/agent-comms-live-20260927-wzjtqhza`, identity `e206f3766e60451a989ca34df0e2a94b`. Native5 stays unchanged for the async checkpoint.

Keep these worktrees/artifacts until publication and borrower checks say otherwise:

- `/home/ts/wt/toad-receiving-native5-batch490-20261001`: contains the **current default** `.artifacts/runtime-certified508-corrected-candidate-20261002` and new310 `.artifacts/runtime-async-selected509-20261002`.
- `/home/ts/wt/toad-viewport-raster-cpu-continuation-20261001`: active308 plus qualified305 `.artifacts/runtime-sidebar-process305-20261002` and public recordings.
- `/home/ts/wt/toad-prompt-action-owner-20261002`: merged307 source, original native journey and combined305/307 staging owned by Schrodinger.
- `/home/ts/wt/toad-merged-selection-sidebar-20261002`: merged304 receiving proofs and fallback candidate; activation metadata corrected to reference the source proof, with the actual journey receipt separate.
- `/home/ts/wt/comms-selected-native-compaction-budget-20261001/stack/.pi-native-5184ffa6d842fe23`: currently used native package; preserve it.
- Parent432 `.release-private/merged297-505-certified508-20261002/publication.json` records the **already executed** default publication. Never rerun that one-use publisher.
- Parent432 `.release-private/merged503-305-process-20261002/` is prepared and **not executed**. Read-only preflight refused one active owner; no defaults/root/packages/owners changed.

## Next actions and current issues

1. Schrodinger has qualified the merged305/307 frontend at `/home/ts/wt/toad-prompt-action-owner-20261002/.artifacts/runtime-sidebar-prompt-actions-20261002` (Toadf207/Coread7/Text2382/Native5184, normal69). Its installed saved-history, warm return, rename, queued input, SendNow click, reply and draft journey passed with four controlled localhost requests. Parent has prepared `.release-private/merged503-305-307-prompt-20261002/`; it has not executed. Its preflight refused the still-open public Toad client3149961 before mutation. Inspect and preserve drafts before a normal quit, then publish at an actual idle window; preserve live work, sessions, drafts, original wire and uncertain inputs. No format reset or input replay for Native5.
2. Kepler has packaged310: Coreca4 / Toad26b425 (merged307+305) / Textual2382 / Native5184, normal69 packages. That journey has ended in private `/home/ts/wt/b509c01`: three configured saved forks, wave3 per owner, concurrent readers and650 unrelated retained rows. The raw driver receipt timed out. Arendt reports all nine original claims completed by43.03s and three original full-turn proofs; the driver waited for a current cursor to remain FULL after later relevance turns advanced it. Arendt is checking the original proofs and correcting the existing observer; preserve the raw failure and do not replay its inputs. No public input or default mutation. This is independent of the Native6 compaction carry.
3. Einstein/Arendt are removing the proposed global provenance digest because23 actual historical ContextManifest refs lack it. The existing frozen InputTaskFact.source/StoredInput.digest owns content identity. Mendel’s carry preserves original bytes. No compatibility default, silent rehash or original rewrite.
4. Heisenberg’s latest308 deletes mirrored channel row fields, shares the original publication owner and reduces repeated class/style updates. Its changed physical public recording at `/home/ts/.cache/agent-scratch/sidebar-owner308-public-20261002-01/capture` has finished: exit1, original owner epoch unchanged, cleanup empty. One predicate failed (`held_page_down_admitted_newer_source`): the observer discarded within-page start/stop offsets. Heisenberg corrected all NativePhase consumers through the existing TranscriptPageAdmission and reassessed the same original recording, with raw exit1 preserved separately. Scoped308 is now Ready at063c4981: warm16/input7 and18 ready body identities. CPU was73.8–87.2% scrolling,31.22% mid-history idle and22.66% at End; the baseline is unmatched, so this proves no overall CPU gain. No new capture or provider replay; full performance scope remains open.
5. The active `compaction499-live-architecture-memory` owner was doing newer authorized context-inspector work, not a dead compaction test. Einstein verified live native tool progress. Do not kill it based on the old test name. Planning-only309 is a separate user instruction, not permission to implement the inspector.
6. Missed-message report: canonical sends347 and349 from that thread to `agent-comms-ux` exist; actual message notifications mark both **Responded**, busy=false. User did not see receipt feedback. Record this as a display/feedback discrepancy; publication alone does not prove what the user saw.

## Full scope and working rules

Continue the full original goal, not only the recent checkpoints. [15-GOAL-CHECKLIST.md](15-GOAL-CHECKLIST.md) retains the original T2/T3/T4, workspace/resource, lifecycle, compaction, scrolling and S1/S3/S4/S5 scope. The full prior requirement audit is `/home/ts/.cache/agent-scratch/toad297-command-source-20261002/FULL-ORIGINAL-GOAL-CLOSURE-20261002.md`.

Read [OPENHCS-HISTORY.md](OPENHCS-HISTORY.md), the latest NRA/refactor-audit archive and [the standing prompt](../../../.pi/APPEND_SYSTEM.md). One fact, one existing owner; migrate every consumer and delete the copies. Use shared behavior, hooks, mixins, metaclasses and context managers where they remove work. Source reasoning first; proportionate tests and the actual installed path last. Speak plainly. Ship useful checkpoints; CI is deferred. Preserve dirty shared checkouts and other agents’ work. Clean only verified owned disposable output.

Existing configured multi-owner driver for the509 journey: `/home/ts/wt/comms-channel-reply-batch-policy-20261002/tests/shared_bus_restart_native.py`. Use a new owned stage with `--owners 3 --collective --wave-size 3 --configured-pure-channel --configured-owner openhcs-helper --configured-peers openhcs-helper2 openhcs-architecture-memory`, the exact receiving Python and native package. The original `/home/ts/wt/b503507c01` is evidence: never rerun or replay its inputs. Arendt owns the new journey.

## Watching tests and the actual UI

Workers can read stdout/stderr during a running test through the execution session. For visual checks, the existing Toad `tools/performance/capture_live.py` exports actual Textual screens and records profiles; the physical terminal capture journey records rendered frames. Inspect those frames together with the profile. A passing test log does not establish smooth scrolling or correct visible feedback. Use an isolated terminal/display for interaction; do not seize Tristan’s desktop or mouse.

## New live goal failure — 2026-10-02

Diagnostic `677e7d84a1694f5aa74f932501e14c0d` is from `agent-comms-ux`. The installed `FailedTurnEvidence` declaration adds `model_request_failed` to the SQL reason constraint, while the original live goal ledger lacks it. A read-only comparison found that this is the only schema-object difference; both versions report `GoalAttemptSchema(1,6)`. Successful `comms_set_goal` is followed by the goal account opening that ledger and failing its schema assertion. The registry goal remains stored and active (`aa78e918e8e24c5abcb9666dc586b339`, revision1). Do not resend the original input or remove the ledger.

Arendt owns source PR513 in `/home/ts/wt/comms-goal-ledger-declared-schema-20261002`; Mendel owns preserving installation PR514 in `/home/ts/wt/comms-goal-ledger-schema-carry-20261002`. This Native5 blocker must not wait on Native6 work. The next release must preserve goal ledger rows and qualify its target schema alongside the other durable stores. No live fix has been applied yet. Publication of the pending305/307 cohort is paused until the existing installation machinery also carries and verifies this ledger; that candidate has the same schema mismatch. This is a demonstrated incompatibility, not an optional check.

The required synchronization belongs to the existing declaration/schema/install owners. Derive the target from GoalLedgerTable and TypedTable declarations, preserve existing rows through the shared carry mechanism, and cover all generated constraints affected by family growth. Do not require the user to perform manual SQL, maintain a second per-table schema list, or separately coordinate every new failure case.

Batch related source corrections first, then batch checks around the affected complete workflow. Every check must name a concrete failure it catches. “Best practice” is not a reason. Source reasoning comes first, validation last; use the actual OpenHCS PR examples rather than adding ceremony. This direction was sent to all seven workers.

## Current publication order

The old305/307 one-use publisher remains unexecuted and superseded by the next combined cohort. Kepler owns one receiving stage with merged509/511/308/310 plus the closed513/514 goal schema and installer changes. Mendel supplies the declared-schema carry; Schrodinger owns one installed saved-fork/native/ACP/physical goal journey. Parent publishes defaults after that path is verified. Native6 lifecycle506/489/carry510 and full performance311/Textual22 continue independently; neither is an artificial gate for this Native5 release. Heisenberg may record changed renderer performance while publication is paused; parent coordinates the actual capture lifetime before cutover.
