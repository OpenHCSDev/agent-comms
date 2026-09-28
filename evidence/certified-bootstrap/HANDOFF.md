# PR251: certified bootstrap and native source reader deletion

## Integration

Branch: `refactor/certified-source-bootstrap-20260928`.
Target: PR229's `refactor/canonical-bus-retirement-20260928`.
Parent `7ba2676` is merged without conflicts. RegistrySnapshot publication checks,
finite wire codec, direct postcommit scheduler callback, admission floor/access,
and PR250's version-derived unread cache filename are retained.

This assigned source batch is ready for integration. Global L0 completion,
combined suite closure and installed activation remain parent/Nietzsche work.
No live root, installed package, user UI or paid provider was changed.

## Source and caller closure

- Publisher initializes the claims marker and existing checkpoint before committing
  its pending registry guard. Empty roots certify at sequence/offset zero; a failed
  seal cannot expose committed registry ownership. The existing installer accepts
  the already-held bus lock; no nested lock acquisition or second builder.
- Native source witness is mandatory PrefixWitness. Coverage uses only bounded
  certified addressed pages. Deleted the 8 MiB hashed tuple and 1,000-row whole-bus
  alternatives, their caps and obsolete tests. Existing 100/page and 32-page work
  budgets remain. Admission floors cannot fabricate native proof; selected claims
  without native input still stop coverage.
- Deleted the separate Messaging/Publisher claim initializer, WireLog claim gate
  mutator and all callers. Initial publication requires the current certificate;
  archived writes fail as read-only before certificate checks.
- ACP marker validation requires an explicitly configured matching certified root.
  Deleted public None-mode, session-mode switch, unused bind_owned mode arguments,
  public drain branch and uncoordinated manual-compaction branch. New/retained
  session attachment and manual canonical compaction are exercised on real paths.
- Production files: publisher, private_bus_checkpoint, native_source_cursor,
  proven_source_coverage, wire_log, messaging, acp, session_lifecycle,
  session_effects, input_drain, input_effects, manual_compaction_bridge.
  Associated bootstrap/checkpoint/native/ACP fixture callers are updated or deleted.

Relative to parent `7ba2676`: production **76 added / 218 deleted**;
tests/guards **156 added / 251 deleted**. New guard tests forbid the removed
reader, cap, initializer and mode mechanisms. No parallel registry or adapter.

## Acceptance

All final checks below ran after integrating parent `7ba2676`:

| Evidence | Result |
| --- | --- |
| final-checkpoint.txt | 57 passed: checkpoint, bootstrap, cutover/admission, seals and source guards |
| final-native.txt | 32 passed, 2 failed, 1 skipped; includes actual pinned Pi fresh/postfloor read/edit/write/bash, publication, claim release and native cursor proof |
| caller-corrections.txt | Both failing cases corrected and passed: obsolete retained-alias rejection removed while preserving owner-generation proof assertions; addressed sealed no-wake page-budget fixture corrected. Manual case initially skipped for missing fixture environment |
| real-acp.txt | 2 passed: configured new/retained ACP attach on the same certificate, and actual ACP manual compaction using the selected journal/authority path; required native environment set |
| final-guards.txt | 6 passed: source closure guards and debt-ratchet behavior |
| final-retained.txt | All 8,540 actual retained messages (120 + 8,400 + 20) preserved byte-for-byte in owned-copy certification; all admission floors retain native coverage zero |
| ratchet.json | No increase in any of the three R0 measures relative to parent7ba2676 |

The native shard's separate digest test was skipped because its dedicated
AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE variable was unset. No full-suite green
claim. Native execution used the pinned Pi package with a local deterministic
HTTP provider, no paid provider calls. Tests were serial within each bounded
shard; at most two independent checks ran concurrently.

Failure receipts are retained: archived error ordering was fixed; an attempted
persistent native test root hit the runtime's existing /var/tmp-only restriction
before provider launch, then existing ext4 /var/tmp disposable fixtures passed;
the first retained staging path disappeared, and a repeat found our existing
stage directory. Correct source and clean owned stage passed. No timeout or cap
was enlarged. Production scope does not alter the parent's runtime-root policy.

Reproduce with the reused integration .venv, PYTHONPATH=src, -n0, -o addopts='',
and bounded timeout. Native cases require AC_NATIVE_COPIED_PACKAGE; canonical
manual compaction requires PI_COMPACTION_TEST_PACKAGE. Both pointed at:
/home/ts/.local/share/agent-comms/native-compaction-policy-b3a9c06/node_modules/@earendil-works/pi-coding-agent.
The retained-copy acceptance script is retained_probe.py beside this handoff.

## Stores and cutover

- private_bus_checkpoint.sqlite3: existing derived source certificate. Fresh roots
  build it once before guard commit. Existing current roots must already be
  certified by the parent's one-shot cutover using this same installer. This PR
  does not reset/rebuild an uncertified existing root automatically.
- bus.jsonl: durable authoritative history, unchanged for retained sources.
- bus_meta.json and .registry-owner-guard: existing identity/authority files; fresh
  initialization now commits the complete current contract together. No new schema,
  parallel store, data conversion, native proof cursor seed or user-history reset.
- Coordinator/native inputs/cursors and unread caches: no reset or schema change
  introduced by this batch. Parent owns D22 quiet activation and tool deletion.

## Ownership and conflict guidance

Nietzsche PR248 owns older ACP handler/text-backend fixture closure. In test_acp.py
this branch only deletes the explicit claim-initializer call; preserve Nietzsche's
canonical fixture edits. Other affected tests must also omit that removed call.
Keep canonical manual-compaction caller assertions with bind_owned's current
signature. Parent candidate wrapper cleanup was inherited, not independently
edited. No history_views, view_unread or cache-path changes in this delta.
