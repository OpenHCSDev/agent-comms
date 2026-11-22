# Plan: background runs for agent-comms

Status, 2026-11-22: built and on main (#5074–#5080). One thread is open: the live test, which
needs an install on a bus host. The first draft of this plan carried 170 lines of code to type
in; that code delivered no notice on either path and is gone. This file now holds what a
hardened plan holds (skills/plan-hardening/SKILL.md): deliverables, prior art, owner map,
family map, dry-run log.

## Deliverables

| Thread | True in the tree when | State |
|---|---|---|
| A thread starts work detached | `run_background` and `background=true` on any tool answer at once with a run id | done |
| The thread is told how it ended | a `BackgroundRunNotice` reaches the thread's inbox with the outcome and the log tail | done, `tests/test_background_runs.py` |
| A bridge restart loses nothing | the next tool touch watches live runs again and reports ended ones once | done in tests; **live step 4 open** |
| The state says what happened | exit code, deadline kill and a vanished process are different states | done |

## Prior art and reuse

Nothing was downloaded; everything below already existed in the tree and is reused, not rebuilt.

| Need | Reused | Where |
|---|---|---|
| A process that cannot be confused with a later one under the same pid | `ProcessIdentity` | `src/agent_comms/execution/child_process.py:395` |
| Wait for exit, signal a whole group | `Platform.wait_any_exited`, `terminate_group`, `force_group` | `src/agent_comms/execution/child_process.py:742,987` |
| States with declared successors | `LifecycleState`, `Terminal` | `src/agent_comms/nominal/lifecycle.py:15`, `lifecycle_capability.py` |
| Core telling a thread something, as a line that starts a turn | `OwnerNotice` | `src/agent_comms/task_attachments/owner_notices.py:41` |
| A durable document under the bus root | `SingleFileStore`, `RuntimeFile`, `LockedStore` | `src/agent_comms/storage/` |

Two facts the first draft did not know, both found by running the real send path:
`Messaging.send` refuses a kind that is not sendable (`messaging/sending.py:140`) and a thread
messaging itself (`messaging/channel_targets.py:508`); and `wait_any_exited` returns at its
timeout, it does not raise.

## Owner map

| Concept | Owner |
|---|---|
| The run family, its states, the store, the watcher | `src/agent_comms/transport/background_runs.py` |
| The two tools | `src/agent_comms/transport/tools/background.py` |
| `background=true` on every tool | `ServedRequest.started_in_background`, `src/agent_comms/transport/tool_request.py` |
| The `background` argument | `ToolRequest.background`, `src/agent_comms/transport/request.py:98` |
| The notice | `BackgroundRunNotice`, `src/agent_comms/task_attachments/owner_notices.py` |
| Tool registration | the import in `src/agent_comms/transport/tools/served.py` |
| The store file | `background_run_records.json` under the bus root; logs in `logs/` beside it |

## Family map

```
BackgroundRun                    run_id, thread, log, process (ProcessIdentity), started_at,
 ├─ ShellRun                     state, reported      command, seconds; exit code in <log>.exit
 └─ ToolCallRun                                       tool; process is the hosting bridge

RunState (LifecycleState)
 ├─ RunningState                 → any ended state
 └─ EndedState (Terminal)        at
     ├─ ExitedState              code
     ├─ KilledState              the deadline passed and the group was killed
     ├─ ReturnedState            a tool call's result is in its log
     ├─ RaisedState              a tool call's error is in its log
     └─ GoneState                the process ended and left no outcome

BackgroundRunStore               start → recover, record, watch
                                 recover → watch unwatched running runs, report, forget
                                 watch → wait, settle, report   (one thread per run)
                                 settle → only a running run changes state
                                 report → claim unreported ended runs in one transaction
```

Consumers hold `BackgroundRun` and `RunState`; nothing outside the family tests a member.

## Decided

1. **Who speaks** — Core, as an owner notice. The first draft's plain sender name and the
   later non-conversation kind are both gone.
2. **Restart recovery** — on touch: both tools and every `background=true` call recover first.
   No bridge-startup edit.
3. **Deadline** — the tool's `seconds` (default 3600), counted from the recorded start, so a
   recovered run keeps the time it had left.
4. **Exit code** — the session leader writes it beside the log. The command author adds nothing.
5. **Retention** — a reported run is forgotten a day after it ended, with its files.

## Dry-run log

Each step, the file it touches and the check that catches its failure. All run in
`tests/test_background_runs.py` (15 cases, about 4 s) except the last row.

| Step | Check |
|---|---|
| Exit code and tail reach the thread | `test_a_shell_run_reports_its_exit_code_and_output_to_its_thread` |
| Deadline kill | `test_a_run_past_its_deadline_is_killed_and_says_so` |
| Ended while the bridge was down | `test_a_run_that_ended_while_the_bridge_was_down_is_reported_at_the_next_touch` |
| Still alive after a restart | `test_a_run_still_alive_after_a_restart_is_watched_again` |
| Process vanished | `test_a_run_whose_process_vanished_without_an_exit_code_is_gone` |
| Pid reuse | `test_a_reused_pid_is_not_adopted_as_the_run` |
| Tool call returns, raises, bridge died | the three `tool_call` cases |
| Reported once | `test_an_ended_run_is_reported_once_however_often_it_is_touched` |
| A real bridge restart, a real session woken | **not run: the live test below** |

Known edge, stated not fixed: a call from another bus with `background=true` decodes the reply
as the tool's own result (`src/agent_comms/transport/remote_core.py:215`) and fails to decode
the run handle. The owner's UI never sets `background`.

## The live test (open; needs an install on a bus host)

1. `agent-comms install`. Delete the old `background_runs.json` under the bus root: it has no reader.
2. `run_background` with `sleep 5; echo done; exit 3`, keep working. Expect a line in this
   thread within about 5 s saying `exited with code 3`, with `done` in the tail.
3. `run_background` with `sleep 90`, then `background_status`. Expect `running` and the tail.
4. Restart the bridge while the 90 s run is live, then `background_status`. Expect `running`
   again, then one `exited with code 0` line when it ends.
5. `run_background` with `sleep 9999`, `seconds` 5. Expect `hit its deadline and was killed`.
6. Any tool with `background=true`. Expect `returned its result` and the result in the tail.

## Done-when

The six live steps pass in a session on a bus host.
