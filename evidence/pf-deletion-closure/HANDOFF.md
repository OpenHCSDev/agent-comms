# PF3 / B3 / B4 deletion closure — ready for parent integration

Branch: `refactor/pf3-deletion-closure-20260928`.
Tree: `/home/ts/wt/comms-pf3-deletion-closure-20260928`.
Base: origin/main PR224 `a10655d`.
Parent owns merge, installed acceptance, pins and live state. No live mutation,
owner restart, helper, paid provider or new native install performed.

## Implemented requirements / deleted mechanisms

### PF3 evidence owner

- Deleted `_read_native_context_evidence`, no alias/re-export.
- `NativeContextProof.read_evidence` owns lookup validation before file access.
- Migrated all five production sites: live `_verify_context`, SelectedExecution
  final verification, historical inputs, continued-session proof, dead-attempt
  failure recovery. Tests call the owner directly; malformed lookup tests cover
  the public owner boundary. Strict file/journal validation and UNKNOWN unchanged.

### B3 live response authority

- Deleted optional `owner_pid` and optional `owner_witness` contracts from prepare,
  publish, resolve and their internal settlement/final/replay helpers.
- Deleted `_require_live_registry_owner` and its `owner_pid is None` fixture bypass.
- `LiveResponseOwner.require_live` owns the exact process/turn, registry status,
  incarnation, recipient and admission checks. All production callers use it.
- Response fixtures now register real current-process owners and obtain actual
  registry turn leases, matching production. Added rejection for ended turns,
  stopped owner, wrong PID/recipient and missing active turn. No manufactured
  acceptance or SQL-only permission; Tx1/dispatch/Tx2 and lock order preserved.

### B4 typed claim path / caller closure

- Deleted `normalize_existing_file` and `normalize_claim_file`, no aliases.
- `FileClaimPath.resource` is a parsed Path. ExistingFileClaim/WritableFileClaim
  determine required filesystem behavior; normalization rechecks actual physical
  paths at claim/write authority boundaries.
- Selected write plans carry ExistingFileClaim; persisted JSON and CLI raw paths
  are parsed at their input boundary. SelectedExecution/foreground/selected-tool
  callers pass the nominal owner. ClaimAdmission no longer accepts raw str/Path.
- CodingTool parses its claim once when the external tool call is decoded; removes
  repeated resource_claim reconstruction and redundant string-type validation.
  Original Pi argument payload remains for exact event/socket correlation.
- Publisher and Messaging accept Sequence[FileClaimPath]. Narrow paired seams are
  explicit in `parent-publisher-seam.patch` (APPLIED in this branch); no parent
  delivery/cutover/read-ledger behavior changed. Publisher calls normalized on
  the owner directly. All raw claim fixtures, including the real subprocess
  interruption script, migrated to current owners.
- Release resources remain canonical claim identifiers: a claim must be releasable
  after its file disappears. `_release_resource` performs actual release-boundary
  validation; it is not the removed file-acquisition coercion adapter.

### Additional bounded PF2 deletion

- Deleted private_bus_checkpoint._failure exception factory; all checkpoint sites
  raise the existing RelationViolationError directly, preserving messages/causes.
- PF1/2/4/5 source audit is in AUDIT-PF1-PF5.md. This batch does not assert that all
  original C0/S1-S8 requirements are complete; parent ledger owns that comparison.

## Local acceptance (xdist disabled; no CI gate)

Python: `/home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python`.
Use `PYTHONPATH=src ... -m pytest -o addopts='' -n0 ... -q`.
Every shard bounded at 45/60 seconds. Receipts kept, including failed attempts.

| Receipt | Executed surface | Result |
|---|---|---|
| native-focused.log | native evidence, native Pi, failure recovery, continued sessions, prompt binding | 133 passed / 7 opt-in skips |
| actual-local-rpc.log | prepared Pi CLI + loopback-only configured fixture, live input/context events | 1 passed |
| checkpoint.log | checkpoint seal + canonical private index | 59 passed |
| response-runtime.log | real response bus/SQL boundary + SelectedExecution + new rejection cases | 76 passed |
| typed-claim-paths.log | nominal physical paths/transition cases | 12 passed |
| selected-claims.log | claim admission, selected tool hook, foreground (including process cases) | 24 passed |
| tool-claims.log | coding, native tool lifecycle, socket broker | 59 passed |
| acp-recovery.log | ACP private selected paths + native UNKNOWN recovery | 47 passed |
| claim-publication.log | full envelope integration, claim baseline, checkpoint after typed caller migration | 58 passed |
| actual-native-four-tools.log | real prepared Pi SelectedExecution; local deterministic read/edit/write/bash; response+release | 1 passed |

Rows overlap: these are receipts, not a unique-suite total.
`tool-claims-first.log`: 56 passed / 3 failed because AF_UNIX fixture paths exceeded
kernel limit beneath the long worktree path; rerun in short owned /var/tmp fixture
passed59. No assertion weakened. Owned copied fixtures cleaned after exit.

Actual native commands set only:
`AC_NATIVE_COPIED_PACKAGE=/var/tmp/agent-comms-pi-native-extensions-20260928/node_modules/@earendil-works/pi-coding-agent`.
Cases: `test_native_pi.py::test_copied_cli_private_policy_allows_one_local_http_attempt[configured]`
and `test_selected_execution_native.py`. Test servers run on 127.0.0.1 with disposable
fixture credentials/model config; never route to the real paid provider.

`deletion-receipt.json`: current source/tests contain none of the four replaced
interfaces. `CHANGED-FILES.txt` lists exact production/test paths.
`changed-path-lint.log` passes. Broader lint retains existing unrelated warnings in
coordination_store.py, claim_admission.py and the interrupted-writer fixture; no
blanket repository-lint claim. No changed-line lint warning remains.

## NRA / audit strength

Read current NRA skill and owner overrides. Manual direct consumer migration,
not an NRA-authenticated codemod transaction. `nra.json` scanned initial PF3/PF2
changes with full package context:79 detectors,0 omitted,complete compact mode,
0 findings,21.2s. B3/B4 were assigned afterward: the scan is NOT evidence that those
new changes or all original plans are globally clean. Their evidence is the direct
caller/deletion audit plus concrete local boundaries above. No zero-findings gate.

## Remaining scope

No known source blocker in assigned PF3/B3/B4. Parent must integrate the narrow
publisher/messaging claim hunks with its separate bus work and run installed
acceptance. Darwin's channel/catalog/tools.py changes were not edited. Actual
provider/runtime deployment and pins remain parent-owned. No new scope inferred.
