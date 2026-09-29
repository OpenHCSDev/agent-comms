# Retained-history checkpoint closure

289 operator lines deleted; no src changes. One-use archived data cutover completed live by the parent using reviewed bb36a974; the converter is deleted. Current regressions/evidence are ready to merge. See LIVE-APPLICATION.md for the precise installed/live boundary and parent receipt.

## Verified cause and completed ownership

Reported live core3f06022 accepted the main checkpoint; both attached archived sources still declared initials/addressed REFERENCES initials while the current declaration derives delivery_sources. Ordinary historical-page access failed at the canonical checkpoint barrier. The parent completed the one-use cutover without runtime compatibility: the same source roots/keys retain original provenance, public history, audiences and archived admission floor, while strict canonical indexes and inode bindings are current. Full original proof-bearing directories remain durable off the active manifest.

The removed operator validated the ten response-only receipts, preserved their original proofs and published only their identical public envelopes because no historical audience existed. Ten initial-only records gained the declaration-owned policy tag with exact original audience/decisions/digest. No historical audience, active wake, admission or uncertain-input disposition was created.

Authoritative NRA and exact refactor-audit archive used: TIME-9, BOUND-1, IMPL-1/4/5/14, IDEN-1/3 and AGENT-8. Retired-format translation existed only in the now-deleted one-use operator; current regression fixtures use canonical owners, not an alternative decoder.

## Evidence boundaries

- canonical-ui-red.log: original unmodified installed reader reproduced the exact user schema error on saved copies.
- cutover-ui-final.log: both complete representative directories, 8,420 public rows, original audiences/proofs; installed fresh-fork DM and actual archived channel-message compositor paint; no test live bus writes or pending delivery. This was the pre-removal acceptance at bb36's implementation lineage.
- activation-rollback.log: historical operator acceptance, including actual directory rollback on injected ENOSPC. This operator is deleted; no test imports its former code.
- LIVE-APPLICATION.md: parent's actual successful installed-core archive application and 56 original-file preservation receipt; live #comms/#nra/#openhcs reads pass. Live UI entry fix remains separately owned.
- post-cutover-current-history.log: surviving current-format saved-state regression using canonical schema/snapshot/history projection; no live archive mutation.
- one-use-census.json: historical operator ownership census; zero chain/codec/foreign-absence/raw-key growth. No product changes.

Both archived transcript annotation schemas already matched current declarations and were preserved. No additional data converter was needed.

## Current reproduction

    ACTUAL_COMMS_SOURCE_ROOT=REPORTED_LIVE_ROOT .venv/bin/python -m pytest \
      tests/retained_history_cutover_pilot.py -q -o addopts=''
    TMPDIR=$PWD/.artifacts ACTUAL_COMMS_SOURCE_ROOT=REPORTED_LIVE_ROOT \
      TOAD_TEST_HELPERS=OWN_TOAD_TREE/tests \
      timeout 90s .venv/bin/python tests/attached_history_installed_pilot.py

Current saved inputs are read-only; all fixture copying, registry/checkpoint inode binding and ordinary snapshotter writes stay in the owned persistent ~/wt. The installed UI pilot needs the paired Workspace/CommsScreen entry fix. Parent owns that actual affected live entrypoint and deployment gate; this receipt does not waive it. CI deferred.
