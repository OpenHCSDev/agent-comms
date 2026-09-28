# PF2: checkpoint seals and private bus marker ownership

## State and integration boundary

Complete source replacement, based on main `03e942d` (includes PF1/PR215).
Parent owns combined installation and live acceptance. No provider calls, live
writes, owner restarts, or history replay were performed for this change.

**Concurrent PF4 caller:** Pascal's new `ActiveRoute.observe_root` must read
`marker.root_id`, replacing `marker["wire_root_id"]`. Apply the attached
`pf4-marker-consumer.patch` while integrating PF4; preserve his full observation
implementation. `PF4-CONTRACT.md` describes the boundary. The private marker read
retains its filesystem guards and adds no service construction or lock.

No remaining PF2 source blocker. DM receipt/activity presentation, Ready during
triage, PF3 native evidence, and PF4 route scheduling remain with their assigned
owners. This PR does not claim to resolve those separate live symptoms.

## Determining owners and deletions

- `wire_metadata.py`: one typed declaration of public/private/claim/checkpoint
  markers, root identity, durable sequence, protocol availability and seal binding.
- `checkpoint_seals.py`: `PrefixSeal`, `CheckpointSeal`, `FinalSeal`, `PendingSeal`
  own declared saved shapes and final/pending validation and recovery selection.
  Existing `DeclaredFamily`/`FieldCodec` derive the family and external state tag.
- `wire_log.py`: one marker decode/publication boundary. Removed independent
  claim/read/sequence marker decoders and `uses_private_protocol_unlocked` /
  `_read_last_sequence` forwarders; callers use typed values directly.
- `private_bus_checkpoint.py`: `PrefixWitness` derives device/inode/offset from
  its revision. `PrefixCertificate` declares the actual SQLite row and derives
  schema/insert/update columns; `FieldCodec` decodes once. Deleted `_witness_record`,
  `_valid_witness_record`, `_final_seal`, `_pending_seal`, `_write_seal`,
  `_check_final_seal`, `_revision`, `_set_certificate`, and the duplicate version
  constant. No raw status switch, witness dictionary, or hand-maintained SQL
  column roster remains. Recovery keeps the existing canonical scan/transaction.
- `registry_store.py` and `acp.py`: removed independent marker shape/version
  rosters and decoding; retain their actual guard and filesystem checks.

### Direct caller closure

| Surface | Migrated files |
| --- | --- |
| Guard, route, cutover and owner identity | `acp.py`, `active_route.py`, `registry_store.py`, `owner_lifecycle.py`, `thread_management.py` |
| Claim admission, sends and publication | `claim_admission.py`, `cohort_foreground.py`, `cohort_send.py`, `coordinated_runtime.py`, `coordination_response.py`, `nk_foreground.py`, `publisher.py`, `selected_write_plan.py` |
| Cursor, coverage and maintenance | `native_source_cursor.py`, `proven_source_coverage.py`, `candidate_maintenance.py`, `wake_candidate_index.py` |
| Checkpoint producers and consumers | `wire_log.py`, `private_bus_checkpoint.py`, new seal/marker declarations |
| Current tests | `test_private_bus_checkpoint.py`, `test_private_human_ingress.py`, `test_supervised_cutover.py`, `test_acp_private_nk_delivery.py`, new `test_checkpoint_seals.py` |

The coordinated-runtime and owner-lifecycle edits each replace only one marker
root lookup; preserve the parent's activity work. No direct marker/witness API
caller was found in the inspected current Toad production/test tree. Raw JSON
tamper tests intentionally still inspect external saved bytes.

## Preserved authority and saved data

Existing JSON marker/seal spellings and SQLite certificate columns are unchanged.
No saved-format migration or live recertification is required. Decoding a seal is
not authority: the current inode revision, root, canonical prefix, final binding,
lock order, transactional checkpoint update, fsync and recovery checks remain.
Pending recovery scans canonical bytes and cannot turn UNKNOWN into a replay.
Malformed markers (including boolean integer impostors, missing fields, null
absence, unknown fields and inconsistent seal/protocol combinations) fail closed.
Required missing/dangling marker files are rejected.

`saved-boundaries.json` records read-only source checks and owned-copy acceptance:

- Original public bus: 8,400 rows; private predecessor: 20 rows; current private
  checkpointed bus: 75 rows. All 8,495 rows and marker projections preserved.
- The saved checkpoint certificate row roundtrips exactly. A copied-inode seal
  correctly fails authority verification. Only the disposable clone was passed
  through the existing full canonical installer to certify its new inode.
- Clone canonical bytes/digest/sequence remain equal; all 46 recipient lookups
  and 1,613 addressed rows match the copied original index with paged reads.
- Original file revisions unchanged, no live writes, copies cleaned. Do not
  delete or rewrite live checkpoint authority when installing this source.

## Local acceptance

`case-inventory.json` lists **264 distinct cases** across 21 modules. Each has a
passing recorded execution; overlapping runs are not added to the count.
Tests use `-o addopts=` (no default xdist) and bounded 60/165-second subprocesses.

| Receipt | Result |
| --- | --- |
| `seals-and-callers.txt` | 86 passed, exit 0 |
| `checkpoint-certificate-final.txt` | 34 passed, exit 0; includes large canonical checkpoint/certificate cases |
| `cursor-integration.txt` | 12 passed, exit 0; complete large paging/native cursor/UNKNOWN cases |
| `authority-consumers.txt` | 56 passed, 1 transient admission-contention failure; that same cohort case subsequently passed in seals-and-callers |
| `acp-registry-cutover.txt` | 79 passed, 7 failed before remaining rename marker callers were migrated |
| `rename-current-contract.txt` | All 8 rename cases passed after migration and obsolete participant-generation assertions were repaired |
| `final-marker-guards.txt` | 52 passed, exit 0; final 30 seal tests, registry and nonrename ACP cases |
| `saved-boundaries.json` | Exact real saved-data boundary checks passed, all copies removed |
| `lint.txt` | Focused Ruff passed; git diff whitespace check passed |

Earlier failed/timeout receipts are retained honestly. `callers-first.txt` names
a nonexistent test file and is not acceptance. `callers.txt` caught an accidental
extra writer-permission check on ordinary read barriers; the original scope was
restored and its cases passed. `checkpoint-cursor.txt` hit its initial combined
60-second bound (exit 124); the complete separated batches above passed.
`rename-final.txt` exposed two stale participant-generation test assertions,
subsequently migrated to the actual declaration. No compatibility API restored.

NRA inspected the full package context with all 79 detectors, no omitted shards.
The checkpoint witness mirror was removed. Three remaining broad family lexical
reports concern unrelated goal/MCP boundaries, inspected and left with those
owners. This is a manually implemented ownership migration, not a claimed NRA
DSL equivalence proof or a detector-count completion proxy.

## Parent next action

Merge this source with PF4's typed root lookup, preserving PF4 route behavior and
parent presentation changes; package and run the assigned installed acceptance.
No native package rebuild, data conversion, provider rerun, or CI wait is required
by this source handoff. Retain branch and receipts; disposable PF2 saved copies
have already been removed.
