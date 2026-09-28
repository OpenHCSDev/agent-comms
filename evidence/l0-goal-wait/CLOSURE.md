# L0 goal/input closure receipt

Draft PR239 targets parent PR229. Parent 121f526 is integrated, including S12's
current GoalHistoryEntry schema and S13's process identity/liveness ownership.

## Retired and preserved

- Deleted AcpDeliveryCursors, DeliveryDocument and DeliveryCursor, and all production callers.
- Deleted Retry's private goal adoption and acceptance of unbound/unaligned waits.
- Recorded BlockedGoal requires an actual bounded reason. UnrecordedBlockGoal
  represents only missing historical evidence through the existing state codec/store.
- Historical mention absence grants no present peer binding. Explicit relationship
  declarations retain their independent meaning.
- Canonical standby inspection uses existing DeliveryScope/current source identities.
  Existing native STARTED observations and explicit reviews handle exact inputs;
  read ACKs and informational cursor coverage do not. Missing attempts are reviewable
  without inspection writes; only a successful explicit review records UNKNOWN
  plus GoalInputDecision in the existing input store. No automatic replay/grant.
- InputDocument schema remains version 1 unchanged. Notices use current owner queue
  facts; absent queue facts are unobserved and grant no new dismissals. Full durable
  input and history preservation contract/counts: CUTOVER.md and both inventories.

## Acceptance

174 tests passed in 35.55 seconds on the parent121f526-integrated tree:
`parent-integration.log`. Earlier 175-pass run predates that integration; S12 deleted
one private history codec test, now covered by actual current-store roundtrips.
Ruff on every changed Python file and git diff --check pass.

Evidence includes actual SQLite history/private goal ledgers, persisted UNKNOWN
and review records across reopen, the actual owner UNIX socket for notice dismissal
and Retry refusal, canonical source publication/review, and owner terminal callbacks.
Backend-failure event fixtures remain focused simulation; this is not a provider
or installed-live acceptance claim. No paid calls, live install, live writes, or
parent-worktree changes were performed. Tests ran serially with bounded timeouts.

Compared with parent121f526: production +170/-218 lines (net -48); tests +555/-490
(net +65). Thirteen named test functions added and thirteen removed/replaced.
The test increase adds real store/socket proof for missing-authority and unknown
historical facts; deleted cursor/drain/internal-golden scaffolding is not retained.

## Ownership closure

Parent owns one-shot conversion tools and quiet activation, including Comms/Toad
schema installation together. Pascal owns current owner input routing during selected
DM work; this branch changes neither InputDrain nor runtime projection. awaiting_keys
call signature remains unchanged. Parent/Pascal have exact external test closure:
`test_acp_channel_disposition.py`, `test_acp_input_disposition.py`, and
`delivery_owner_fixture.py` still reference the removed public drain/cursor engine
in the parent snapshot; those engine fixtures are outside this assigned batch.
No source shim or old reader remains in this batch.

## Exact implementation files

- `src/agent_comms/goal_actions.py`
- `src/agent_comms/goal_failure_observation.py`
- `src/agent_comms/goal_management.py`
- `src/agent_comms/goal_presentation.py`
- `src/agent_comms/goal_states.py`
- `src/agent_comms/goal_waits.py`
- `src/agent_comms/input_attempt.py`
- `src/agent_comms/input_disposition.py`
- `src/agent_comms/relationships.py`

## Direct tests

- `tests/goal_owner_fixture.py`
- `tests/test_goal_block_reason.py`
- `tests/test_goal_failure_observation.py`
- `tests/test_goal_input_review.py`
- `tests/test_goal_nominal.py`
- `tests/test_goal_standby.py`
- `tests/test_goal_standby_liveness.py`
- `tests/test_goals.py`
- `tests/test_input_delivery_current.py` (deleted)
- `tests/test_input_delivery_history.py`
- `tests/test_input_disposition.py`
- `tests/test_input_documents.py`
- `tests/test_l0_goal_input_closure.py`
- `tests/test_locked_store.py`
- `tests/test_relationships.py`
