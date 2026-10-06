# Goal-report preservation source checkpoint

The existing GoalHistoryStore now owns full read-only acquisition: original
private regular file, declared SQLite schema, complete typed rows including
owner_created_at/state, and stable acquisition. It never calls __init__,
_initialize, history or observe. Ordinary live goal history behavior is unchanged.

GoalHistoryEntry now declares the family discriminator `row`, because `kind` is
already its original journal field. This closes the actual full FieldCodec
encoding collision. SQL fields/DDL and the original public to_wire projection
are unchanged. Current and explicit amended720 P share this declaration/method.

GoalReportMemberRetirement replaces the public raw thread/threads/releases
strip helpers. It consumes original-decoded registry/release carriers and the
full same-root journal. Same-incarnation committed history owns the accumulated
report across Clear/Set/Edit; a release may match an earlier committed endpoint.
Pending/uncertain, observed gaps, missing/conflicting endpoints, or a baseline
purporting to supply a model report refuse. Aborted rows remain in full acquisition
and cannot authorize the report. No journal row is inserted or rewritten.

ThreadRetirementCutover and its original preflight acquire this relation before
fencing and again under all-stopped custody. The target decodes the complete
registry/release postimages and every full journal row, and compares its own
read-only acquisition before source replacement or any launch. The original
journal preimage is retained with the other operation preimages, without changing
the original journal. Live capture uses the same relation. Current same-format
capture and recorded provenance remain distinct and retain their existing owners.

The fixture producers no longer set manual report strings. Original model goal
actions and Registration commit the report and full revision history. Pilots
consume that acquired projection and preserve journal bytes. All callers of the
removed public projection helpers have migrated, including original preflight.

## Actual source checks

* Current: 4 read-only acquisition checks and 3 changed-owner refusal controls PASS.
* P: 4 read-only acquisition checks PASS in the preserved initial batch.
* Paired P producer/current strict target: 11 report/postimage and refusal cases
  PASS after the row discriminator correction (reported, clear, replacement,
  edit, older release; missing report, pending, uncertain, wrong incarnation,
  gap, report-bearing baseline). Original full registry still refuses target
  decoding; no reader fallback is added.
* P: 3 changed-owner controls PASS after migrating original refusal cause/custody,
  exact source launch declaration, and the missing test import. The original
  real Python children remain alive at changed-owner refusal; no replacement is
  launched, and failure custody is explicitly abandoned before teardown.

Source-only paths/imports are explicit in goal-report-source-controls/results.json
and its two correction subdirectories. All initial negatives are retained:
full-row codec collision, missing original fixture launch declaration, then the
missing P StoppedOwnerFailure test import. No earlier passed acceptance was
repeated. Test children are fixture-owned Python processes, not native/agent work.
These controls do not qualify all-stopped real OFD, install, launch, recovery,
public admission or genuine cross-format live central-batch acceptance.

Owned small authored source scratch is retained for these source witnesses at
/home/ts/.cache/agent-scratch/einstein-goal-report-source-controls-20261006,
/home/ts/.cache/agent-scratch/einstein-goal-report-source-controls-resolved-20261006,
and /home/ts/.cache/agent-scratch/einstein-owner-release-source-import-resolved-20261006.
No original stored history/root/prefix, provider, SDK/native package, build or
installed operation was acquired. Frozen routing source/artifacts/closed leases
remain unchanged. P is amended original source, never unchanged720/c3e equality.

Paired explicit amended720 producer P source: `1c5ac5a86a5c8550bf82004bff1289658b72184f`.
