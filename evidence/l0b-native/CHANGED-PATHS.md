# L0B owned production paths and deletion map

- `src/agent_comms/acp.py`
- `src/agent_comms/agent_loop.py`
- `src/agent_comms/backend.py`
- `src/agent_comms/input_drain.py`
- `src/agent_comms/native_entries.py`
- `src/agent_comms/native_pi.py`
- `src/agent_comms/native_session_reopen.py`
- `src/agent_comms/owned_turn.py`
- `src/agent_comms/owner_compaction_adaptive.py`
- `src/agent_comms/session_lifecycle.py`
- `src/agent_comms/turn_inputs.py`
- `src/agent_comms/turn_progress.py`
- `src/agent_comms/turn_runner.py`
- `src/agent_comms/worker.py`
- `pyproject.toml`

## Removed implementations and current owners

| Removed | Current owner / migrated consumers |
|---|---|
| backend.rpc_args_for, raw argv task/stdout text engine | NativePiRpcLaunch + TurnSession; ACP/session/turn/input/manual arguments |
| Participant, ParticipantEventConsumer, agent_loop independent ACK/poll loop | worker.main + canonical session owner; published agent-comms-agent |
| native_session_reopen.package_for_launcher | NativePiRpcLaunch.package_for_command; saved/native/compaction callers |
| old text/classifier/Participant success fixtures | explicit native RPC contract fixtures and actual pinned CLI/refusal/headless/stdio |
| direct_interrupt test driver and removed pending ticket constructors | actual SelectedExecution + existing InputDrain inbox, current native followup tests |
| fresh human input borrowing parked goal grant/wait | exact current queued input receipt/goal/wait/owner fences, goal permits remain goal-only |

Launcher scenarios retained, no alias. S13 current marker/process fixtures integrated.
Current test changes include native_event_host.py, conftest.py, test_backend.py,
test_agent_events.py, test_pi_rpc_nominal.py, test_maintenance_native_boundary.py,
test_native_only_execution.py, test_native_only_guards.py,
test_native_owner_launcher_resolution.py, test_native_session_reopen.py,
test_pi_payloads.py, test_acp.py, test_acp_private_nk_delivery.py,
tests/fixtures/private_native_cursor_v1.json and test_selected_owner_followup.py.
Removed tests/test_agent_loop.py and tests/test_acp_owner_interrupt_followup.py.
The cursor golden follows current S12 fields; no runtime compatibility reader.

Remaining coupled path: manual_compaction_bridge.py, Darwin PR236. Parent owns paired
Toad/current metadata changes and deployment. No other worker's live tree edited.
