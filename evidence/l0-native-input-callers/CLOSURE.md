# L0 canonical input caller closure

Parent explicitly transferred these three files after integrating PR239:

- tests/test_acp_channel_disposition.py
- tests/test_acp_input_disposition.py
- tests/delivery_owner_fixture.py

Base: parent290104d, which contains PR239. No production, converter, parent
worktree or live-state changes in this batch. The old public drain/cursor fixtures
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

Backend model execution in channel tests uses the existing _fake_model fixture;
all source/store/routing/projection logic is real. Direct ACP tests use an actual
local scripted RPC process through the existing opt-in native_rpc_fixture. Neither
is a provider or installed-package acceptance claim. No paid calls.

## Results and integration defect

Focused run: 7 passed, 2 failed in4.40s (focused.log).
Separate permanent guard:1 passed (guard.log).
Ruff and git diff --check pass. The prior174-test batch was not repeated.

Both remaining failures are actual HistoryViews.message_notifications calls:
parent290104d history_views.py162 queries native_runtime_inputs/n.claim_id, but
current S12 declaration is NativeRuntimeInput (native_runtime_input, assignment_id).
_project_notifications also references removed OwnerLifecycle._process_alive.
Exact evidence/reproducer was sent to parent codex-bootstrap for the existing
notification source owner. Those assertions remain; no skip, compatibility table,
or patched notification result masks the source defect. This draft is not green
until parent integrates the source correction and these two tests pass.

Pascal retains selected-DM fresh owner routing. This batch exercises fresh ACP
follow-ups during an ordinary owner turn and does not duplicate Pascal's selected
DM implementation. Parent retains all conversion and activation work.

## Deletion

Compared with parent290104d:800 lines deleted,401 added (net -399).
15 named test functions retired/replaced;7 named tests added, with10 parameterized
cases total. Tests of deleted batch/cursor structure are gone; current behavior
coverage and the guard remain in the three assigned files.
