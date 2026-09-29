# C3 owner-state cleanup

Owner: Mendel. Receiving scope from Core419 C2, based on its tested 8c4afabe
checkpoint. Source assignment: cleanup-2026-09-29.zip README/C0/C3. PR414 test
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

Parent owns canonical TurnProgress/OwnedTurn backend state and Core/Toad
permission/status/cancel integration. Coordinate backend.py, goal_actions.py and
thread_status.py against that contract at its implementation checkpoint. No private
active_turn probes, reserved-flag projections or symptom branches. Independent
families continue while that contract is implemented.

## Authorities and exclusions

Reuse parent Core418 WireValue checkpoint 11748ae3 through a normal merge for
external error decoding: to_wire/from_wire, optional wire_schema; FieldCodec
consults that capability. Do not mark recursive FieldCodec-on-self adapters.
Scalar FieldRepresentation remains canonical.

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
