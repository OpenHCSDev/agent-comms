# R5 runtime/collaboration document ownership

2026-09-28 · Darwin · source complete, parent owns integration/activation.

Base: main193 `bd4b84d` (includes accepted R2/core192 and R4/core190).
Branch: `refactor/runtime-collaboration-documents-20260928`.
Owned tree: `/home/ts/wt/comms-runtime-collaboration-documents-20260928`.

## Owners and deletion closure

| Surface | Actual authority and migrated callers | Deleted mechanism |
| --- | --- | --- |
| Runtime metadata | `RuntimeInfoStore(LockedStore[dict[str, AgentRuntimeInfo]])`; `FieldCodec` derives fields including external `ts`. `AgentActivity`, `HistoryViews`, `ThreadManagement`, concurrency/operations/declaration tests use `read()` and `path`. | Custom constructor/path mirror, `get`/`all`, `_load`/`_load_unlocked`, three repeated JSON writers, handlisted record serializer/constructor, handlisted rename reconstruction. |
| Collaboration document | `SharedLedger(LockedStore[dict[str, Any]])`; inherited shared snapshots and exclusive updates. `CollaborationLedger` retains registered-author policy with its actual registry dependency. `Comms` constructs its declared document path. Rename/delete still own exact structural reference edits. | `_data` mirror, eager load, custom `read`, `_load`/`_load_unlocked`/`_save_unlocked`, duplicate root/wire-path constructor state. |
| Activity projection | `ActivityCheckpointStore` and typed `ActivityCheckpoint`; source revision, offset, tail hash and identity checks own checkpoint validity. `ActivityLog` captures it while holding the log lock. | Handlisted checkpoint decode/encode/schema roster, raw checkpoint atomic writer, `_checkpoint_path`, unused whole-log `_load`, `Activity.to_wire` field mirror. |
| Snapshot publication | `LockedStore.replace` and shared `_publish_unlocked`; both replacement and update use the existing durable writer. | Update's inline serialization duplicated by checkpoint writes. Replacement intentionally skips decoding the discarded projection. |

Activity is still an append log. Its existing incremental latest/complete/offset cache remains derived from that log; no new store of activity authority. `Activity.from_wire` now only normalizes genuinely absent historical dates to zero before the declared codec. Runtime missing historical dates likewise remain zero. No observation timestamps are invented while reading old data.

Free-form ledger values remain JSON data. No nominal family for arbitrary collaborator keys, no separate registry, no compatibility accessors. Parent found and migrated Toad's test-only `profile_hot_paths_pilot` `Activity.to_wire` call to `FieldCodec.encode`. No production Toad direct store API consumer was found; existing `agents.agent_info_of` protocol remains.

## Lock and persistence contracts

- Document reads share canonical locks; update and replacement hold exclusive canonical locks. All durable writes reuse A8 staging, original mode preservation, atomic replace and rollback on a reported publication/fsync failure.
- Policy calls acquire wire, then registry proof, then runtime/ledger document lock. No callback reads another document. Activity publication orders log lock then checkpoint lock. No reverse acquisition or cross-document transaction is claimed.
- Runtime/ledger/log filenames and actual external field shapes remain. Existing files decode directly; no live migration command is necessary. Runtime's next ordinary write persists zero for an absent historical timestamp. A bad activity checkpoint rebuilds from the unchanged authoritative log; corrupt authoritative documents still raise without being overwritten.
- Ordinary runtime/ledger read-modify-write uses `update`. `replace` is for a complete captured snapshot; the activity source lock remains held through publication. It fixes a measured redundant-decode regression without weakening bounded reparse tests.
- Restart keeps existing owner fencing/lifecycle implementation. The actual local process regression now also proves runtime metadata and nonempty ledger bytes survive restart; it starts `/bin/echo`, never a model/provider.

## Local acceptance

Every listed command exited 0 with `timeout 60`, `PYTHONPATH` set to this tree's absolute `src`, and `.test-venv/bin/python` from `~/wt/comms-historical-views-20260927`. All pytest runs used `-o addopts=''` (no xdist/default coverage gate), `-q`, and owned persistent `--basetemp=/home/ts/wt/.r5doc/<batch>`.

| Receipt | Selected tests | Result |
| --- | --- | --- |
| `owners.txt` | `test_declarations.py::TestAgentRuntimeInfo`, `::TestSharedLedger`, `test_operations.py`, `test_activity_index.py`, `test_locked_store.py` | 125 passed, 6.63s |
| `integration.txt` | `test_runtime_collaboration_documents.py`, `test_restart.py`, `test_concurrency.py` | 29 passed, 6.47s |
| `views.txt` | `test_historical_views.py`, `test_viewer_snapshot_index.py`, `test_acp_compaction_activity.py` | 18 passed, 4.38s |
| `documents-final.txt` | `test_runtime_collaboration_documents.py`, including two added shared-reader/exclusive-writer cases | 12 passed, 0.11s; 10 overlap integration, two new |

**174 distinct focused cases passed.** Bounded activity evidence still requires zero decodes on warm unchanged reads, one after one append, ten latest entries on reopening a 3000-row/10-thread log, and eleven on reopening after a new-thread append. Tests count actual `FieldCodec.decode(Activity, ...)`, including nested checkpoint decoding.

Retained failed attempts: `basic.txt` failed because the owned basetemp parent had not yet been created; `basic-corrected.txt` then passed105/failed2 because read-modify-write re-decoded the old checkpoint. `replace` resolves that actual issue; `owners.txt` proves the unchanged bounds. No aborted/failing receipt is counted as a pass.

NRA `nra-after.json`: full `src` dependency context, six changed persistence owners, 79 detectors analyzed, none omitted, `exact_compact_global`, zero findings. Commands used two parse/analysis workers, 45s scan budget and a 60s process limit. `nra-before.json` was captured during early edits and is not an authenticated original-source comparison. Changes are authored ownership decisions, not a DSL/native-equivalence proof; tests carry the behavior evidence.

## Parent acceptance and next action

Parent reports independent old/new copied saved-document comparison: two current runtime records and two latest activity records identical, shared ledger empty; archived roots had no runtime/activity/ledger files. Nonempty ledger behavior is therefore established by source fixtures, not that real-data comparison. Parent owns paired Toad activity/profile and session-sort pilots and installed activation. No live roots, packages, providers, native bundles or model settings were changed here.

Integrate this branch with the paired Toad test caller migration, build/install through the parent's existing route, and run the planned installed activity/sidebar checks. There is no new data migration, replay or restart instruction in this change. Retain existing runtime/ledger files; checkpoint remains disposable. No source blocker or required R5 follow-up remains. R3 is a separate next assignment; no R3 input/delivery store edits were made.
