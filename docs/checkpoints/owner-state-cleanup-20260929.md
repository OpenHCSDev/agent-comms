# C3 owner-state cleanup

Owner: Mendel. Receiving scope from Core419 C2, based on its tested 8c4afabe
checkpoint, now merged350b14a9. Source assignment: cleanup-2026-09-29.zip README/C0/C3. PR414 test
cleanup is merged; C2 is independently reviewable. No new workers.

## Full acceptance tracked here

Close foreign state reconstruction in all six assigned files:
coordination_response.py, owner_lifecycle.py, goal_actions.py, backend.py,
turn_watchdog.py and goal_management.py. Trace each probe to its actual declaration
owner and consumers. Extend existing typed identity, activity and command owners;
classify genuine optional/external boundaries rather than hiding them in wrappers.

Close all five family sites:

- goals.py mention resolution: states carry their own identity or diagnostic data.
- maintenance_barrier.py: decode one phase lifecycle at the boundary.
- relationships.py: declared add/remove/update commands own edits.
- thread_status.py: command capabilities own control eligibility.
- acp_failure.py: external payload shapes decode once and own detail rendering.

Arendt Core425/Toad211 now owns canonical TurnProgress/OwnedTurn backend state and Core/Toad
permission/status/cancel integration. Coordinate backend.py, goal_actions.py and
thread_status.py against that contract at its implementation checkpoint. No private
active_turn probes, reserved-flag projections or symptom branches. Independent
families continue while that contract is implemented.

## Authorities and exclusions

Parent replaced withdrawn Core418 checkpoint11748ae3 with4951d8d4.
The published API is agent_comms.field_codec.WireValue, inheriting
FieldRepresentation with abstract to_wire/from_wire and optional schema().
The existing representation owner handles encoding; no new codec or type switch.
No production code here depends on either checkpoint yet. Do not mark recursive
FieldCodec-on-self adapters. Scalar FieldRepresentation remains canonical.

Parent owns sealed.py and FieldCodec/PendingRequests/ReadLedger/PiRpcChannel/
ChildProcess seals, typed_table A13 review and C4. Einstein Core417 owns C1 Pi
vocabulary; threads.py and pi_events.py are excluded. Kepler owns TC2 ACP SDK.
Arendt Core416 owns large-context native accounting and usage acceptance.
Mendel's C2 registry_document refusal guard extension is already in Core419.

## Evidence, checks and deployment limits

Patterns: IDEN-3, IMPL-1, IMPL-3, IMPL-5, MEMB-2, BOUND-1, BOUND-2.
Re-measure actual heads; do not use the old 41-chain source snapshot. At2c1ca485
the six state files contain14 long BoolOp chains, five family files another6;
these20 are selected-surface screening leads, not a global count or20 defects.
Record per-function/file StringDispatch/TypeSwitch subjects and distinct arms.
Classify external taxonomies and enforce admitted family sites with guards.

Acceptance uses existing retained-native/ACP private continuous fixtures with
controlled loopback responses. Preserve original input/UNKNOWN/noReplay,
history/settings/correction and owner custody. Source tests, installed entrypoint
acceptance and global activation are reported separately. No paid provider calls,
live-root mutation, global install or owner restart. Scratch owner Mendel:
`/home/ts/.cache/agent-scratch/comms-owner-state-cleanup-20260929`.

Independent obsolete selected-summary child fixture cleanup is a separate
tests-only follow-up; no production compatibility export will be added.

## Tested source checkpoint

Independent families implemented: relationship commands, maintenance lifecycle,
goal mention contact/diagnostic projection. Response publication admission,
preparation and frozen-intent checks now consult the existing RecoverySnapshot,
execution, attempt and obligation declarations. Stored mention keys remain the
current format; unresolved members declare constant absent identity fields.

Serial evidence: response-goal-maintenance-final.log has47 passing checks in7.95s;
relationship-family.log has19 passing checks with3 resource-heavy concurrency
cases deselected. These are source checks, not full C3 native acceptance.
Remaining files and both remaining families above stay assigned here.

The concrete cross-runtime restart queue blocker is a separate C2 followthrough
PR: exact source identity proof and reviewed target runtime must be distinct.
No live queue activation belongs to this source checkpoint.

## Further tested owner operations

External error shapes use existing MroDispatch declarations at one decode
boundary; internal published receipts still use FieldCodec. No custom WireValue
is needed.22 presentation/diagnostic checks pass. Goal/GoalState own failure
blocking and provisional-completion revocation; owner pause, progress and exact
goal/worktree gates remain.61 durable checks and23 focused block/resume checks
pass. Eight fake-stream caller cases require public RequestError expectations,
reproduced on main697b plus Core424; Arendt425 has the caller-closure evidence.

Actual saved native history -> normal ACP input -> autonomous native503 -> typed
published failure receipt -> blocked goal -> passive reopen/noReplay passes in
18.67s (actual-goal-failure.log, native7817). UNKNOWN original remains unchanged.

OwnerReleaseReceipt owns source/admission release proof; RegistrySnapshot owns
fenced stopping identity and delegates executable process proof to Thread.
33 release/process/turn-lease/restart/error checks pass28.07s, including actual
release-before-exit, grace/escalation and stale birth/admission/missing receipt.
The obsolete expected_incarnations fixture now uses OwnerRestartSelection.
A redundant retired startup refusal sentence was deleted; actual private trace,
PublicationActivationBlocked type and owner identity assertions remain.
Watchdog reads existing NativeAttestation.observed rather than reconstructing
attestation from a nullable state field.

Remaining canonical turn/activity/control consumers await Arendt's reviewed425
contract; f05a62e7 is an importable source checkpoint, not full acceptance.
The owner prioritizes the separate sender outbound relation contribution to
Schrodinger210. C3 full acceptance remains tracked here, no scope discarded.
