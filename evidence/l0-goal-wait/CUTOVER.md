# L0 goal/wait/input cutover contract

Owner implementation branch: refactor/l0-goal-wait-closure-20260928, based on
parent PR229 at 14df8d6, with parent 121f526 integrated (current S12 GoalHistoryEntry table). Parent owns the one-shot tools and quiet activation.
No live or parent source/data is modified by this branch.

## Durable goal records

Inventory is in data-inventory.json, obtained read-only (SQLite mode=ro).
Registry/history/wait counts rechecked 2026-09-28 16:24:56 UTC; unchanged.
Live registry: 18 goals, 4 blocked, 3 blocked with absent/null reasons;
13 goals lack mention evidence. Live history: 2 entries, no missing block reason.
Retained root: 20 goals, 6 blocked, 3 missing reasons; 456 history entries with
887 before/after goal snapshots, 104 blocked, 61 missing reasons; 516 snapshots
lack mention evidence. These roots overlap historically; do not sum as unique goals.

After the parent's existing flat-to-current goal conversion, transform each
current blocked state with absent/null block_reason to exactly:

    {"kind": "unrecorded_block"}

A recorded nonempty bounded reason remains {"kind":"blocked","block_reason":...}.
Never use progress, provider guesses or an inferred owner decision as the reason.
No blank, oversized or invalid reasons were observed; stop on any such value
rather than repair it silently. Preserve every goal ID, text, progress, revision,
reported_turn and mention_source, and every history entry's sequence, kind, state,
observed_at, owner identity, before/after association. Missing mention_source
remains absent evidence: do not bind today's peers to yesterday's text.
UnrecordedBlockGoal is a current GoalState variant, decoded by the same FieldCodec
and GoalHistoryStore as all other states. There is no second history reader.
Consumers use its declared behavior; explicit Retry still requires an existing
private generation. No new action can create a reasonless BlockedGoal.

The live root has no root goal-private DB; all 18 registry goals therefore lack a
root-level private generation. The retained root has 2 of 20 registry goals absent
from that store. Do not infer authority from the registry or generate READY grants
as a side effect of retry or this cutover. Preserve existing private ledgers and
surface missing authority explicitly. This inventory does not assert absence of
unrelated per-session private stores.

## Wait state

Live root: no wait file. Retained root: four waits, all four null owner_created_at,
report_turn_id, report_turn_generation and all four missing aligned target turns.
Waits are runtime scheduling state. At quiescent activation reset goal_waits.json
to {}. Never infer past turn identities or recreate waits. Do not turn UNKNOWN
inputs into runnable work; existing private attempt grants remain authoritative.
New GoalWait rows require owner_created_at and a target_turn_generations slot for
every target. A null target slot means the dependency was idle when declared.
Reporting turn ID and generation must both be present or both explicitly null;
null is valid for a runtime declaration made outside a turn. Missing fields and
unbound owners are rejected. S13 process_alive checks are retained.

## Durable inputs and cursor deletion

input-inventory.json counts durable records on both roots at 2026-09-28
16:24:56 UTC: live 35 rows (9 UNKNOWN, 8 dismissed, 26 with native IDs),
retained 7,766 rows (2,577 UNKNOWN, 1,059 dismissed, 5,211 native IDs, 547
review-bearing, 7,209 bus rows). Active live counts are changing; inventory
again at quiet cutover and preserve every row, never require a fixed row count.
Preserve ALL
input_dispositions.json rows, UNKNOWN/STARTED state, exact source/sent text,
owner/admission/native IDs, goal_reviews and notice_dismissed flags. This branch
changes no input-record schema and requires no input-record conversion or reset.
Delete only the disconnected acp_delivery_cursors.json after quiet activation.
Delivery presentation uses the current owner's explicit awaiting_keys snapshot.
Without that snapshot the scope is unobserved; no new notices are dismissed.
Already dismissed evidence remains historical, and UNKNOWN never grants replay.
The old cursor cutoff is not delivery proof and is not copied into new authority.
Standby uses canonical direct dependency messages scoped by the existing
DeliveryScope.current incarnation check. Existing InputAttempt STARTED evidence
or an explicit goal review handles an exact input; a transport/UI ACK cannot
substitute. A canonical message with no input record is projected read-only as
an UNKNOWN observation using its exact source text and the current owner admission.
Only a successful explicit reviewed_inputs action persists that observation and
GoalInputDecision, atomically in the existing input document. Failed/partial
reviews and inspection alone write nothing. Existing rows are compared before
review, never overwritten by reconstructed facts. No native ID, sent text,
STARTED state, scheduling entry, private grant or replay permission is inferred.
Selected native SQL receipts and informational cursor coverage are not converted
into input-attempt STARTED receipts; absent handling evidence requires explicit
review. Old sender/recipient incarnations stay in history and are not current
standby dependencies.

## Integration ownership

Parent/Pascal own InputDrain and runtime projection plus old cursor-engine tests.
Their existing calls providing awaiting_keys remain the API. This branch owns
input_disposition.py, input_attempt.py, goal_management.py, goal_states.py,
goal_actions.py, goal_waits.py, relationships.py and their direct tests.
Minimal capability/presentation caller closure touches goal_presentation.py and
goal_failure_observation.py. S13's integrated process liveness is preserved.
The parent must install the shared current goal schema in Comms and Toad together.
