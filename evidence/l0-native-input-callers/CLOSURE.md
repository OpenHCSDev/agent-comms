# L0 canonical input caller closure

Parent explicitly transferred these files after integrating PR239:

- tests/test_acp_channel_disposition.py
- tests/test_acp_input_disposition.py
- tests/delivery_owner_fixture.py
- src/agent_comms/history_views.py (subsequent production ownership transfer)

Base: parent290104d, which contains PR239. No converter, parent worktree or
live-state changes in this batch. The old public drain/cursor fixtures
are deleted, including the unused queued_delivery_owner helper. The replacement
uses existing canonical source setup, ACP owners, current coordination/native
SQL declarations, and an actual owner socket. Permanent AST guard prevents the
removed fixture authorities returning.

## Behavior retained on current owners

| Retired fixture assumptions | Current evidence |
|---|---|
| Batched channel inputs copied into InputDispositions | Per-source/per-recipient canonical assignment decisions and historical native proof, including IGNORE versus FULL; distinct input IDs and exact prompt equality |
| Cursor-based UNKNOWN and ACK handling | Actual coordination uncertainty with no native receipt, no replay after viewer ACK, and explicit channel notification projection |
| Public queued DM goal/project/stop checks | Canonical selected reservation held before send; actual goal/project/stop change denies send and leaves durable native observation unproved |
| Late subscriber reading a queued bus disposition | Actual owner UNIX-socket subscription during a native RPC turn; fresh ACP follow-up remains UNKNOWN after ACK |
| STARTED versus ACK-only native input | Actual local child pipes and JSON RPC; two distinct native IDs, matching start required; ACK-only remains UNKNOWN across reopen and notice dismissal |
| Public drain crash before/after cursor | Actual separate process exits after durable current owner acceptance; exact text retained, no native ID, reopened owner creates no replay queue |
| Text-backend behavior/preflight fixtures | Current native launch boundary, actual RPC preflight rejection, visible failure, zero prompts sent |

Backend model execution in channel tests wraps the existing _fake_model fixture
with a distinct file for each fresh session, preventing later inputs from
overwriting earlier evidence;
all source/store/routing/projection logic is real. Direct ACP tests use an actual
local scripted RPC process through the existing opt-in native_rpc_fixture. Neither
is a provider or installed-package acceptance claim. No paid calls.

## Notification defect fixed after production ownership transfer

The initial failing run is retained in focused.log (7 passed, 2 failed) and
guard.log (1 passed). Neither failing assertion was removed or weakened.

Both failures were actual HistoryViews.message_notifications calls:
parent290104d history_views.py162 queries native_runtime_inputs/n.claim_id, but
current S12 declaration is NativeRuntimeInput (native_runtime_input, assignment_id).
_project_notifications also references removed OwnerLifecycle._process_alive.
history_views now derives table names from NativeRuntimeInput, WakeClaims and
CurrentExecutions, joins through assignment_id, and uses Thread.process_alive
to validate PID plus process start identity. AssignmentState.notification remains
the behavior owner. Callers receive the existing decoded WakeAssignment rather
than reading raw identity fields again. Reads remain bounded and read-only;
missing evidence cannot become successful delivery. No compatibility tables or
removed liveness helpers remain in this path.

Current checks (serial, local, PYTHONPATH=src, pytest -q -o addopts=''):

- tests/test_acp_channel_disposition.py + tests/test_message_notifications.py:
  9 passed in 3.11s (notifications.log).
- tests/test_acp_input_disposition.py: 5 passed in 2.49s (input-owner.log),
  including the permanent retired-fixture guard.
- Ruff on the four changed source/test files and git diff --check pass.

The new real-owner test suspends canonical execution before native send and
checks both message and agent-view notifications. A current process reports
Responding; the same PID with a different persisted start identity reports
Paused without launching another input. Original per-recipient IGNORE/FULL,
uncertain receipt/no replay and late owner socket tests remain intact and pass.
The prior 174-test batch was not repeated. Copernicus owns installed Toad
acceptance; these checks do not claim live installation or provider execution.

Pascal retains selected-DM fresh owner routing. This batch exercises fresh ACP
follow-ups during an ordinary owner turn and does not duplicate Pascal's selected
DM implementation. Parent retains all conversion and activation work.

## Deletion

Combined source/test delta against parent290104d: 813 lines deleted, 478 added
(net -335) across the four owned files listed above; evidence files are separate.
Initial test closure compared with parent290104d: 800 lines deleted, 401 added
(net -399). The notification follow-up replaces the stale SQL/liveness callers
and adds one integration test plus session-isolation fixture support. The added
test covers process-incarnation correctness across both user-visible views.
15 named test functions retired/replaced; 8 named tests added, with 11 parameterized
cases total in the assigned tests. Tests of deleted batch/cursor structure are
gone; current behavior coverage and the guard remain.
