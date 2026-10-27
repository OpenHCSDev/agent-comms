# agent-comms: the owner's unowned asks, on the board

Opened: 2026-10-25, at `7f0923fca` (main).

## The thesis

An ask Tristan made that no order holds lives only in someone's memory, and memory is a second copy that disagrees with nothing until it is lost. Each ask here is a phase: the heartbeat board prints `START owner_asks/<phase>` for it until its evidence holds on `origin/main`, so the ask has one owner, the board, and nobody restates it.

## Phases

Each phase and its evidence on `origin/main` are declared in `src/agent_comms/plan_phases.py` (`OwnerAsksPlan`). No phase waits on another.

| Phase | Ask | Done when, on main | Now |
|---|---|---|---|
| `keymap_k2_k7` | Toad keymap K2 to K7 (toad#1212, `KEYMAP-PLAN.md` in Toad): remappable F1/F2, bindings that change with focus, a PC-game-style binding menu (K7) | this document records the merged Toad commit (below) | not recorded |
| `page_quarter_scroll` | plain PageUp/PageDown in Toad scroll a quarter page; toad#1233 did only Shift+PgUp/PgDn | this document records the merged Toad commit | not recorded |
| `loading_spinner` | Toad's top loading spinner is back; both were removed instead of keeping the top one | this document records the merged Toad commit | not recorded |
| `commander_latency` | each `agent-comms-commander` verb answers in under 5 s (about 30 s now) | `test_commander_verb_answers_under_five_seconds` declared in `tests` | absent |
| `delivery_latency` | a bus message is injected into the recipient's next tool call; delivery slowed at about 01:57 UTC on 2026-10-25 | `test_message_injects_into_next_tool_call` declared in `tests` | absent |
| `ram_budget` | one worker host holds every unit owner as an object; stop multiplying package memory by the owner count | `test_fleet_memory_stays_under_its_cap` declared in `tests` | shared-host plan below; implementation and test absent |
| `unflatten_modules` | plan the package structure of the flat modules in `src/agent_comms`, after the fleet is stable | class `PackageStructurePlan` declared in `plan_phases.py` | [package plan](PACKAGE-STRUCTURE-PLAN.md); implementation has its own declared phases |
| `board_line_lock_text` | board lines say "LOCKS your writing verbs" although #3345 made `HeartbeatGated` (`commander/order.py:843`) print the board instead of refusing; the text must derive from what the gate does. JUDGE lines embed each verb's full argparse usage | no `LOCKS your writing verbs` in `src` (now `agent_comms_hooks/board.py:34`); no `verb\.usage\(\)` in `commander/standing.py` (now `:167`, `:382`) | 1; 2 |
| `closed_unmerged_prs` | a standing rule flags a PR closed unmerged that still holds commits nobody landed (lost: toad #1080, #1095, the commander board page) | class `ClosedUnmergedPullRequest` declared under `src/agent_comms` | absent |

### Toad asks

Phase evidence reads only the plan repository's `origin/main`, and Toad's tree is not in it. Until an evidence kind reads another repository's main, a Toad phase is done when the Toad PR has merged and its merge commit is recorded here as a line of its own, in the list below, starting with a dash, the phase name, the word `landed:`, and `OpenHCSDev/toad@` followed by the full 40-character merge SHA. The pattern is declared once, on `ToadAskPhase`.

Landed:
- page_quarter_scroll landed: OpenHCSDev/toad@24badf03f60e829abd3ed3383262ef7db669ccca
- loading_spinner landed: OpenHCSDev/toad@1ce4872a8a8cf3ae988b6f2ff5a28b9fe1f6f81a

### Latency and memory asks

Each is done when a test pins the target, so a regression fails the suite rather than an owner's patience. The test's name is the evidence; the test must measure the real path (a commander verb end to end; a message sent to a live recipient and seen in its next tool call; the fleet's resident memory against a declared budget on its owner).

### ram_budget: one host, many owners

Implementation owner: `unit-rambudget`, PR #3393. Commander direction: bus message
29756, superseding the cold-import diet. This is the plan checkpoint, not a
working shared host or completion of the phase.

**Target.** One `agent_comms.worker` OS host for the selected private bus holds
every unit owner as an object. Start/stop adds or retires that owner's object;
it does not start/kill a Python interpreter for each unit. One host imports the
package, owns the root's coordination worker, and shares store reads by revision
across the hosted owners. Different private roots remain isolated. Native model
children may still run concurrently; removing duplicate control processes must
not serialize their turns. No per-process hard cap, lazy-import shim, or parallel
old/new owner system is the deliverable.

**Measured starting point.** The 2026-10-26 census counted 29 owner processes,
5781.58 MiB summed PSS and 6399.96 MiB summed RSS. The separate cold-import probe
loaded 919 modules: 112.15 MiB PSS, 132.06 MiB RSS, 2.418 s. These samples do not
prove the owners were all idle, identify a heap leak, or establish the shared
host's eventual cost. The installer separately reported 1067.705 s in owner
start (bus message 29751); this plan does not treat that elapsed time as a heap
profile or a proved RAM-gate call chain.

#### Process state that must stop standing in for an owner

Source references below are pinned to main `7c93b48d94d08d7a13baa8632938edd211ce3670`.
The AST pass used refactor-audit's `Repository.python_files` over `src`, `tests`
and `tools`: 1037 Python files, zero parse omissions. It enumerated module/class
bindings, explicit global/nonlocal declarations, context variables and process
identity/environment/signal calls; the sites were then read through their
consumers. Dynamic dispatch, native code and third-party globals are not proved
complete by this syntactic pass. OS-wide state is listed separately from Python
module bindings; neither should be disguised as an owner-local field.

| Fact currently supplied by process scope | Declaration/write and consumers | Shared-host owner |
|---|---|---|
| Selected bus root and startup arguments | `launch_root.py:23-32,43-49,70-80` installs `LaunchRoot._current` and removes root arguments from `sys.argv`; `worker.py:26,104` selects it; `active_route.py:170` and `config_options.py:192,224,267` read it | The host selects one root once. Owner work uses its existing `Comms.route`; never switch the process root between tasks. |
| Calling unit name, worktree, tags, parent, task and managed flag in `os.environ` | `launch_runtime.py:402-463` declares/reads `ThreadLaunch`; `worker.py:36,112`, `threads.py:642-653`, `owner_lifecycle.py:470,562` and `runtime_children.py:35` read the ambient caller | The owner object's decoded `ThreadLaunch` and recorded thread identity. Pass the object through execution/release/child-start paths; construct a child's environment explicitly rather than rewriting the host's environment. |
| One-use authorized startup input removed from process environment | `launch_runtime.py:466-471` strips the startup-input field; `worker.py:51-55,60-79` schedules its exact key | The individual owner-start request consumes its key once, using the existing input record/claim check. Host restart must not discover or replay UNKNOWN/failed inputs. |
| Native binary, argument policy, configuration/auth directories and inherited environment | `native_arguments.py:319-322,427-465` decodes environment; `launch_runtime.py:267-370` owns `RestartEnvironment`; `owner_lifecycle.py:517-523` builds launch inheritance; `native_pi.py:533-540` reads it for a native child | Preserve the existing typed launch/configuration values on each executing session and pass them into native launch. `TurnRunner`'s one-agent launch defaults (`turn_runner.py:110-112`) must not make all hosted units inherit one unit's settings. Do not copy credentials into a new store. |
| Working directory and process output streams | `owner_lifecycle.py:524-532` launches the worker with `cwd=thread.worktree` and `OwnerProcessOutput`; `diagnostics.py:31` declares that output; `launch_runtime.py:448` falls back to `os.getcwd()` | The registered owner's worktree and diagnostic output. Native children receive that cwd/output explicitly; the host never calls `chdir` to run another owner. |
| PID/birth time as both OS process and one owner's lifetime | `owner_lifecycle.py:311-326` captures/reserves the calling PID; `registration.py:130` reserves a process; `child_process.py:258,288` tests local PID/release; `owner_lifecycle.py:433-491,570-575` signals and releases owners | Keep OS identity on the host, and owner identity/generation on the existing registry owner. Start/stop requests name the exact owner; stopping one must never signal the shared host. PID liveness alone must not mean every hosted owner is active. |
| Runtime endpoint selected by PID | `owner_connection.py:25-26,93-105` derives the socket from PID; `runtime.py:242` binds it; `acp.py:202-205` creates a proxy from the owner's PID | One host `RuntimeServer` routes the already thread-addressed request envelope (`owner_connection.py:41-46`) to the exact hosted owner, preserving incarnation/generation checks. Do not bind competing servers to the same PID path. |
| SIGTERM/SIGINT as one unit's stop and whole-agent shutdown | `worker.py:38-49,56` binds both signals to one `OwnerStop`; `turn_terminal.py:102-126` owns the stop reason; `turn_runner.py:110` supplies one stop to `owned_turn.py:309`; `session_lifecycle.py:391` releases all bound/launched owners on shutdown | Per-owner stop belongs to that owner's session state; host signals retire all owners through the same individual retirement behavior. An ordinary stop must not call whole-agent shutdown. |
| Restart reconstructs one owner's configuration from `/proc/PID/environ` | `owner_launch.py:20-70` captures process environment, validates `ThreadLaunch` and derives binary/arguments; `owner_restart.py:58` consumes the captured configuration | The hosted owner's existing typed launch values must be obtained through its checked owner request. A shared host environment cannot identify which owner's configuration to restart. |

#### State already scoped correctly: extend it, do not duplicate it

- `CommsAgent` already owns session-aware components (`acp.py:149-200`).
  `SessionLifecycle` owns bindings, titles, worktrees and proxies
  (`session_lifecycle.py:71-85`); `InputDrain.drained` owns `DrainedSession`
  objects and their independent locks, queues and tasks
  (`input_drain.py:447-467,582-608`); `TurnRunner.persistent_backends` selects
  backends per session (`turn_runner.py:114-115,154-167`). Extend those owners;
  do not create a second agent, queue or writable session mirror per hosted unit.
- `Coordination._worker` is already a context variable
  (`coordinator.py:99-113`), not a mutable current-unit global. One host/root
  owns its store worker. `StoreLock._releases` and
  `StoreLockAcquisition._held` (`store_files.py:178,631`) remain operation-scoped
  when crossing executor threads; they must not become host-wide held locks.
- `RuntimeServer.controller` (`runtime.py:239-241`) is already request-scoped.
  `NativeSession.active` (`backend.py:215`) is keyed by the actual asyncio task.
  Preserve those scopes so concurrent units cannot borrow another controller,
  native child or selected-write permission.
- `RegistryCache._by_path` already shares decoding by path/revision
  (`registry_store.py:120-139`). Reuse it. `InputObservation.observe`
  (`input_drain.py:192-219`) currently starts a watcher and reads a snapshot per
  session; move root revision watching/reading to the host and fan that single
  immutable result to the existing owner drains. A shared process alone does
  not delete repeated reads.

#### First cut and completion checks

1. Put a host lifetime on the existing `worker.py` entrypoint: one `Comms`,
   coordination worker, `CommsAgent`, runtime socket and signal registration.
   Extend the existing `DrainedSession` execution capability to hold an owner's
   typed launch values, one-use startup key and stop reason. Derive executing
   membership from those owned sessions, deleting the competing
   `SessionLifecycle.launched_owners` roster rather than adding a host mirror.
2. Change `OwnerLifecycle` start/ensure/stop/release and the existing owner
   request family together. They add/remove an exact registered owner in the
   host. One host launch is reserved once; subsequent starts are object
   acquisition, not `SpawnedProcess.launching(... agent_comms.worker)`.
   Preserve registry generation checks and release evidence; remove per-unit
   PID signals, per-unit worker environments and `/proc`-as-owner-configuration
   reads in this same cut. Stop-one, restart-one and attachment must work before
   this replaces the current launch path. No fallback per-unit workers.
3. Share root revision observation through the existing read/watch owners;
   preserve each session's input/turn locks, queues, backend, controller and
   reader position. Stopping one retires only its producers/native children
   and leaves all other owner objects running. A host shutdown joins every
   owner's retirement before releasing the host's shared resources.
4. Verify on a practice bus: at least three simultaneously hosted owners share
   one Python host; independent replies, attachment, rename, stop-one,
   restart-one, concurrent turns, host shutdown and saved-history startup work.
   Record host/child PID and birth-time observations, RSS/PSS, module count and
   store-read counts before/after at unchanged and changed revisions. Preserve
   original sessions and uncertain inputs; never replay them as a test.
5. After that real-path measurement, declare the fleet's regression budget on
   its resource owner and make `test_fleet_memory_stays_under_its_cap` measure
   the real hosted-fleet path. This plan checkpoint does not choose an
   arbitrary killing limit or satisfy the phase merely by naming a test.
   Installation and live-message answers remain separate from source/merge
   evidence. No pytest runs are authorized for this checkpoint.

The interrupted HUD extraction left no source changes. It is not retained as a
separate deliverable: moving a display import does not remove 28 interpreters.
