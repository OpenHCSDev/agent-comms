# O1 assignment / turn-lease vocabulary — source closed

Base main202 `83b4f8ddc31bc5810b3fa94666bfd90771e6edbb`.
Implementation `bd10486ce70b964026b4446c8283cce20ff35410`.
Own persistent tree: `/home/ts/wt/comms-r7-vocabulary-closure-20260928`.
Branch: `refactor/r7-vocabulary-closure-20260928`.
Darwin owns this source; parent owns integration and activation.

## Complete caller/deletion map

| Owner | Current contract; deleted spelling |
| --- | --- |
| ACP selected-write callbacks | Three callbacks consume `assignment: WakeAssignment`; no `claim` parameter. |
| OwnedTurn | Stores `turn_lease`; no `turn_claim` field/alias. |
| TurnRunner | Relay local is `turn_lease`; `finish_turn_stream` and `settle_turn` take `lease: TurnLeaseFence`. |
| TurnProgress | Reads `execution.turn_lease`; terminal failure observation passes `lease=`. |
| FailedTurnObservation | `from_terminal(..., lease=...)` is the only keyword. Exact owner/incarnation/turn/admission checks retained. |
| Manual compaction bridge | Captures and settles `turn_lease` through the existing runner. |
| Registration / RegistryDocument / AgentActivity | Turn-acquisition result is `leased`; no turn-result local named `claimed`. |
| Current tests | Direct consumers and assignment helpers use actual assignment/lease contracts. Lease test helper storage is `_test_leases`; no old fixture-only field or alias. SQL field/value fixtures retain real saved encodings. |

Nine production files and17 existing test files changed. Resource claim
operations, resource test fixtures, goal launch permits and external saved/native
claim/epoch encodings retain their meanings. No counter, CAS, lock, permission,
queue, cancellation, UNKNOWN or manual compaction policy changed. The production
edit changes identifiers plus local formatting; every source string literal is
unchanged (`rename-boundary-check.json`). This is a bounded source check, not a
claimed semantic equivalence proof.

The previous full audit is preserved in `evidence/original-plan-completion/`
(cherry-picked documentation commit `b71a47e`). Current committed-source
inspection finds zero remaining O1 identifiers, zero retired API identifiers,
zero internal epoch identifiers and no resurrected aggregate imports/modules.
`source-inspection.json` records exact source revision. No compatibility aliases
or tests for obsolete interfaces were added.

Read-only current paired Toad search found no call with these renamed keywords
or field access; parent already migrated the public lease-method fixture in99.
An older Toad test *name* still mentions turn claim; it is not a core API caller
or a production compatibility boundary. No paired production patch is required.
Pascal's built-in target cleanup may touch TurnRunner's global target constant;
this branch changes only lease locals/parameters in that file, not that constant.

## Focused local acceptance

All exit0, no xdist, existing interpreter, each outer bound60seconds:

- `lease-consumers.txt`:259passed/1skip,24.21s. TurnRunner, terminal error
  observation, shared events/settlement, manual bridge, ACP compaction activity,
  selected write, goal standby, identity/registration, declarations, compaction
  owner gate and read ledger. The skipped platform contract is Windows-only.
- `assignment-consumers.txt`:269passed/1skip,46.43s. Coordination/state/store,
  cohort schema and response, optional awareness, claim-admission verifier,
  native prompt binding and selected execution. Prepared-package digest opt-in
  is skipped in this batch; no claim that this optional case ran here.
- `native-local.txt`:3passed,15.64s. Actual prepared Pi with local fake providers:
  two real ACP queued-during-summary variants (including foreign ingress),
  original/followup each once in order, plus normal FULL read/edit/write/bash,
  one publication and exact lease/resource release. No paid-provider call,
  package rebuild or live state involved.

Interpreter:
`/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python`.
Commands use `PYTHONPATH=src timeout 60 <python> -m pytest -q -o addopts=''`.
Lease module names above match `test_<name>.py`; exact command arrays are in
`commands.json`. Native command selects `tests/test_input_drain_native.py` and
`tests/test_selected_execution_native.py` with these existing read-only packages:

```
PI_COMPACTION_TEST_PACKAGE=/home/ts/wt/comms-refactor-integration-20260928/stack/.pi-native-aef88838db0496c2/node_modules/@earendil-works/pi-coding-agent
AC_NATIVE_COPIED_PACKAGE=/var/tmp/agent-comms-pi-native-diagnostics-20260928/node_modules/@earendil-works/pi-coding-agent
```

No known source/test blocker remains. No broad optional reruns or CI wait.
Formatting was restricted to changed lines after tests; syntax/literal-boundary
inspection covers the final formatting. Receipts and scripts retained; no native
bundle/cache copy created. Parent may integrate this complete change directly.

## Full remaining completion audit

See `COMPLETION-AUDIT.md`: O1 is source-closed; main202/Toad99 live acceptance is
now documented, including parent's fresh source61/reply62 actual read/bash.
This follow-up's installation remains parent-owned. O2/O3 remain explicitly
qualified; no whole-goal completion inferred from source status.
