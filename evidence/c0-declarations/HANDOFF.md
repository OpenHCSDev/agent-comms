# C0 declaration and current-import closure — Darwin

Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/179

Branch `codex/c0-declarations-20260928`, persistent tree `/home/ts/wt/comms-c0-declarations-20260928`.
Based on merged lease174 (`b5ec95a`); integrated parent checkpoint176/main `4216214`.
Parent owns paired Toad import migration, Pascal operations integration and installed activation.

## Delivered / deleted

- Deleted the 4,507-line `declarations.py` aggregate. Moved all78 class/function definitions to their actual domain modules; no duplicate implementation, compatibility re-export module or shared-self mixin remains.
- Removed the moved declaration exports from package root, migrated current source/tests/subprocess fixtures, benchmark/tool/stack scripts and README imports. `tests/test_c0_ownership.py` checks the retired import boundary across active source/consumer directories.
- Existing `channels`, `goal_presentation`, `response_policy`, `read_basis`, `registry_document`, `presentation`, `bus_activity_index`, `thread_identity`, `bus_publication`, `envelope_claim_transitions` and `private_registry_guard` owners are extended. The other real declarations/stores have defining modules. `TOAD-IMPORTS.md` and `symbol-owners.json` are the canonical one-time paired-repo migration map, not runtime registries.
- `goals.py` owns Goal/provenance declarations. Pascal's independent operational component must be `goal_management.py`. Operations edits here are imports only; parent/Pascal migrate their new component imports using the same map.
- MessageBus is relocated intact to `message_bus.py`. Its further S7 decomposition is **not** claimed complete. This C0 change removes aggregate ownership and creates independent files; it does not claim a reduced new-case edit count merely from relocation.
- Canonical lock/file primitives now live in `store_files.py`; the existing bus durability barrier lives in `bus_durability.py` and is invoked after lock acquisition as before. The checkpoint-only MessageBus construction remains inside that barrier. No new lock, cache, bus or allocator.
- ChannelActivity belongs to its existing `bus_activity_index`; presentation consumes it. Thread label values are independent of view aggregation. Display scopes/evidence join existing `read_basis`. The authority modules do not import the presentation aggregate.
- Saved JSON/session formats, identity/lease meaning, UNKNOWN, claim/durability and partial-painted-ACK logic are unchanged. `moved-definition-audit.json` verifies all78 bodies unchanged after disregarding migrated import statements; this static comparison is not a native semantic proof.
- Checkpoint176's actual installer and schema retained from main; diff against main in `private_bus_checkpoint.py` and `supervised_cutover.py` is imports only. No checkpoint migration or live probe was repeated here.

## Focused evidence

All Python tests use the existing historical-views `.test-venv`, this tree's absolute `PYTHONPATH`, `-o addopts=''`, owned persistent fixture directories and60/165s bounds. No CI wait, provider calls, new helpers/models, live writes/restarts or replay.

| Evidence | Actual result |
| --- | --- |
| `core.txt` |185 passed,1 skipped: declaration/registry contracts, channels/saved views, S4, registration and thread identity. |
| `bus-history.txt` |112 passed,1 failed: durable bus/claims, history pages, read evidence, indexes, goal history. Failure was the moved checkpoint fault-injection target, not an installer failure. |
| `correction.txt` |7 passed: corrected checkpoint injection at actual store_files owner, plus S4 structural/read tests. |
| `native-consumers.txt` |67 passed,2 skipped: TurnRunner, session ownership, command families and ACP private delivery. Native queue cases required the prepared bundle; covered explicitly below. |
| `native-queue-corrected.txt` |7 passed, exit0: real local native selected-summary→commit→original→queued input exactly once, with/without foreign ingress; five selected-tool fake RPC variants. |
| `checkpoint-integration.txt` |Initial60s bounded run incomplete after30 success dots, without pytest summary. The shell tail wrapper masked its status; do not call this a completed green suite. Remaining14 cases were run separately. |
| `checkpoint-tail.txt` |9 passed,5 fixture failures: remaining existing-root/certificate/cutover cases. All five failing fake-runtime certificate cases omitted the required saved model. |
| `certificate-corrected.txt` |5 passed, exit0: fixture now declares fake/fake, which is used only by the monkeypatched synthetic runtime. Includes over1000rows/8MiB and10/150-participant certificates. Production model policy unchanged. |
| `ownership.txt` |1 passed: current imports cannot recover the retired aggregate. |
| `collection.txt` |2814 cases collected before176 integration; collection is not execution. |
| `nra-before.json`, `nra-after.json` |Both exact_compact_global,79 analyzed,0 omitted, complete,0 findings. Entire src dependency context; final targets all27 defining/extended modules. |

The first explicit native attempt (`native-queue.txt`) failed before useful acceptance: I supplied a package outside the canonical launcher layout and the long fixture path exceeded AF_UNIX limits. Corrected environment uses the already prepared parent integration stack package `.pi-native-aef88838db0496c2/node_modules/@earendil-works/pi-coding-agent` and `/home/ts/wt/.c0-darwin-queue`. The fixture prohibits outbound fetch and supplies synthetic streams. No native package was built or mutated.

Ruff I/F and current diff whitespace checks pass. Pre-existing PR text retained in the functional evidence may include original whitespace; it is source evidence, not authored product code. Optional broad/full-suite and configured-provider reruns were not performed.

## Functional audit included

Original audit committed/pushed as `e3ee932`, included here as `c9e035d`. `evidence/functional-audit/HANDOFF.md` retains all six requirement findings, all88 saved-session parser/page results and the actual four-tool-result transcript audit. Only metadata/counts/result identities are recorded; no private session content. Parent accepted the checkpoint activation gap and implemented176; a dated integration note distinguishes the old observed baseline from that later work. Existing provider receipts were audited, not rerun.

## Parent handoff

1. Apply `TOAD-IMPORTS.md` to current paired Toad consumers; no old package/declarations aliases are retained.
2. Combine Pascal operations changes serially. Preserve `goals.py` declarations and name his component `goal_management.py`; migrate new component imports to actual owners. No operational behavior was changed here.
3. Build/install/test the combined paired candidate using parent's existing activation path. This worker has not changed the installed runtime. Existing-root checkpoint176 and its saved data remain authoritative.

The direct `codex queue` message to Pascal was rejected because the spawned subagent was unloaded; parent was notified with the file map. No coordination hold or duplicate worker was introduced. Owned test/scanner processes finished; disposable fixtures/cache are cleaned after retaining these receipts and the useful scripts.

Cleanup completed: removed163MiB owned scanner/test artifacts plus the624KiB short native fixture directory. Native bundles, original histories and shared worktrees untouched. Commands are retained in COMMANDS.md.
