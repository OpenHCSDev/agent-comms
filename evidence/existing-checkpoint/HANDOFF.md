# Existing private bus checkpoint activation

Functional completion audit found PR94's source checkpoint absent on the live bus. The existing installer refused nonempty roots, leaving the capped canonical scan active. This change extends that same installer/schema and writer lock to certify a complete existing private bus. No new bus, schema, dual reader, message rewrite or input replay.

The installer builds a private staged SQLite index using the existing canonical row validator, compares the current bus revision and reserved sequence, then atomically publishes the sidecar and durable marker seal. Failed validation/index construction leaves the original bus usable and no active sidecar. An uncertain failure after sidecar publication remains denied by the existing durable read barrier. Bus bytes/inode/root identity are unchanged. Fresh managed cutover calls the installer by default.

## Local and installed acceptance

- checkpoint-tests.log:28 passed, including existing-root byte/inode preservation, continued append, partial row, reserved publication and unattested source refusal.
- migration-cursor-tests.log:four old cursor fixtures failed because no configured model was provided; two cases passed. Corrected fixture to declare the same configured model as other fake-native tests, without adding a production default.
- migration-cursor-fixed-tests.log:5 passed, including existing-root migration beyond1,000rows/8MiB, fake-native source cursor/SQL binding, UNKNOWN/no-replay and index rollback rejection.
- Failed existing index-build test passed in the first cursor batch: injected disk-full leaves original history readable and no staged/index leftovers.
- cutover-tests.log:4passed/1failure because the old no-replay assertion expected a missing bus file. New default certificate requires an empty bus inode; assertion now checks zero bytes. cutover-fixed-tests.log records a basetemp-parent setup error; cutover-final-tests.log:5passed, including default checkpoint, saved identities/goals/waits and unchanged old UNKNOWN/pending inputs.
- Installed wheel live-copy-result.json:copied current52rows/31initials; all46recipient pages match canonical history. Bus bytes/inode, registry and coordination SQLite bytes unchanged. No live mutation, native/provider call or replay. Initial harness failed because copied registry lacked its inode-bound guard; final harness uses the actual MessageBus constructor with read-only original registry dependency. All migration writes stay in the owned fixture; fixture removed.
- NRA before/after:exact_compact_global,79detectors,0omitted; same single pre-existing semantic-mirror finding. Extends existing authority with an authored implementation; not a native equivalence proof.
- Ruff imports/undefined names, diff check and wheel build passed. CI deferred by owner.

## Integration

Based on merged174/175, so wheel also contains the tested exact turn-lease cleanup. Paired Toad87 remains unchanged. Parent owns serial existing-bus installation, links and idle-owner reload, then one fresh real channel check. This handoff records pre-activation evidence; do not claim live migration from the copy receipt.
