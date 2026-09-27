# Restored private subscribers remain deliverable

The roster-only restoration in PR136 imported saved declarations without
coordinator registration. After PR95 installation, fresh source16 could not
seal because its frozen audience included restored observers with no durable
participant identity. No native input for that source had been sent.

`Comms.restore_stopped` composes the existing registry collision checks and
execution-authority stripping with existing coordinator registration under the
wire lock. It registers all selected identities, including already-restored
ones, so retry repairs an interrupted registry/coordinator operation. It does
not start stopped owners, copy historical messages/cursors, or change existing
owner generations. Public roots do not create a coordinator database.

Use this operation instead of the low-level `registry.restore_stopped` in
restoration tooling. The lower-level API remains for registry-only snapshots.
The two durable stores are not one atomic transaction: a coordinator failure
raises after registry restoration, and repeating the operation completes it.

Local validation: 16 tests passed in roster restoration and ordinary N/K delivery;
Ruff and diff checks passed. New cases exercise both fresh restoration and
repair of a preexisting restored audience, its sealed N=2/K=1 receipt, unchanged
live ownership, stopped saved identity, and idempotency. Fake-model ordinary
N/K tests do not establish native compaction behavior.

Live repair used existing coordinator registration for 96 previously restored
identities. Registry and native input rows were unchanged during the repair.
Without resending source16 or restarting owners, the installed PR95 runtime
completed it and published source17 `PR95_RUNTIME_OK`. Receipts are in
~/.local/state/agent-comms/{restored-participants-repair,pr95-live-response}.json.
This proves delivery and response on the installed runtime. Real compaction,
retention, old history and general coding tools remain separate unfinished work.
