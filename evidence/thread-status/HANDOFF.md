# ThreadStatus lifecycle and consumer closure — Pascal

## Ready for parent integration

Tree: `/home/ts/wt/comms-refactor-thread-status-20260928`.
Branch: `codex/refactor-thread-status-20260928`.
Source commit: `26c6346cc3d163220da11c40b9719369ba5bcff7`, rebased without conflicts onto current main171 `2923711` (includes relationship169 and index167). The following handoff commit only records evidence/cleanup. Parent owns paired Toad integration and installation. No provider calls, live changes, new environments or additional workers.

## Actual ownership/deletion

- `thread_status.py`: public `ThreadStatus(DeclaredFamily)` ABC with frozen nominal `RunningThreadStatus`, `IdleThreadStatus`, `StoppedThreadStatus`, `ArchivedThreadStatus`, `DeletingThreadStatus`. Shared active behavior is inherited from `ActiveThreadPresence`. Membership/names/decoding use A1; A2 can compose the nominal values. No second registry, enum mirror, singleton adapter or transition table.
- States determine visibility, activity/running/stopped capabilities, presentation, restoration without replay, deletion eligibility, heartbeat reactivation, mutability refusal, owner-generation changes and context-control eligibility.
- `RegistryDocument` consumes these decisions while retaining atomic document mutation, counter ownership and existing lock order. Running/idle heartbeats preserve both generations; stopped/archived reactivation rotates both; deletion rejects late heartbeat/registration without writing. Restoration keeps archived provenance archived and all other restored owners stopped.
- `Comms` start/restart/stop/release/cutover use behavior rather than enum identity; removed fabricated fallback statuses after owner-presence guards and the duplicate stopped check around the canonical release receipt check. `list_threads`, `thread_detail`, `ThreadView.to_wire` encode the determining declaration name.
- Registration/default construction, snapshot/view typing, goal-failure observation and all current in-repo test callers migrate together. No ACP/TurnRunner implementation, history/index implementation, claims, native policy or deployed roots changed.

### Exact removed public surface

Deleted the enum declaration and its five members: `ThreadStatus.RUNNING`, `.IDLE`, `.STOPPED`, `.ARCHIVED`, `.DELETING`. Deleted enum scalar construction `ThreadStatus("running")`, enum iteration, `.value`/`.name` and the scalar `.mutable` capability. No aliases or adapters retain them.

Current construction: `from agent_comms.thread_status import RunningThreadStatus`; use `RunningThreadStatus()`. Current persisted boundary: `ThreadStatus.decode(saved_name)()`; encode `status.declared_name`. Type annotations/public package export `ThreadStatus` identify the real ABC. Consumers query `.active`, `.running`, `.stopped`, `.visible`, `.in_view(...)`, `.presentation(...)`; mutators use `.require_mutable(name)`, `.for_deletion()`, `.after_heartbeat(name)` and `.changes_owner(next_status)`.

Actual disk/wire status spelling stays `running`, `idle`, `stopped`, `archived`, `deleting`; no registry migration or old-client coexistence gate. Existing saved thread fields, creation identity, aliases, session provenance and generation encoding are preserved. This is one current file schema with a current nominal boundary.

## Paired Toad callers — parent owns the patch

`toad-consumers.patch` is a concrete diff against `/home/ts/wt/toad-live-pins-20260928`, verified with `git apply --check` without changing that checkout.

- `src/toad/widgets/comms_sidebar.py`: remove the enum import. `_person_kind` asks `not person.status.active`; inactive/deleting owners cannot implicitly open a native owner. Deleting is already excluded from the roster. `_show_thread_menu` filters the current catalog through `person.status.allows_control(item["name"], owner_pid=person.thread.pid)`, removing the archived/active status dispatch from Toad.
- Fixtures: `tests/right_comms_pilot.py`, `tests/channel_visibility_pilot.py`, `tests/main_menu_transfer_pilot.py` use nominal construction/behavior.
- Current Toad source has no other core ThreadStatus callers (its unrelated `ThreadStatusRow` widget stays intact).

Do not activate this core with the obsolete Toad enum callers still installed. Parent has explicitly taken ownership of applying/testing the paired patch; no retained compatibility shim is needed. No remaining core implementation blocker.

## Local acceptance (CI deferred)

Use the existing integration venv and **absolute** source PYTHONPATH for child processes:

```sh
PYTHONPATH=/home/ts/wt/comms-refactor-thread-status-20260928/src timeout 60 \
 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest \
 -q -o addopts='' --basetemp=.artifacts/thread-status/<batch> <tests>
```

- `core.txt`: **173 passed, 1 skipped** — declarations, registration components, fresh-process persisted lifecycle, registry revisions, owner/turn/delete/rebind counters, roster restoration, visibility, channel presentation.
- `consumers.txt.gz`: **148 passed, 2 failed initially** — new nominal lifecycle acceptance, operations/start/restart, cohort foreground, supervised cutover, importing. Both failed process cases used relative PYTHONPATH; a child importing from its own cwd resolved the integration checkout. `processes.txt`: both actual local start/restart socket cases **2 passed** with absolute source path. Sessions retained, old worker exited, new worker attached, no input replay.
- `runtime.txt.gz`: **238 passed, 2 failed initially** — runtime, ACP, end-to-end, goal failure/liveness, cohorts, private entrypoint and passive awareness. One child-process import issue as above; one stale shared-lock observer lacked A8's `blocking` keyword. `runtime-fixed.txt`: those exact cases **2 passed** after absolute PYTHONPATH/fixture correction (same fixture fix as relationship169; cleanly folded by rebase).
- The three disjoint batches plus corrected cases account for **563 passed, 1 skipped**. Failed batches are retained honestly; they were not full green runs.
- `rebase.txt`: **57 passed** on main171 — ThreadStatus, registration components, restart (including actual processes), visibility, passive awareness and current viewer index seams. This overlaps the earlier evidence; do not add its count to unique acceptance.
- Ruff I/F and `git diff --check` passed. No further optional testing required.

The focused tests include all five saved status values through fresh registry reads/list/detail/view JSON, A2 composition, inactive stale-activity presentation, active/idle deletion refusal, deleted-owner write refusal, current context controls, preserved process/turn/identity boundaries and actual child process/socket paths. No configured-provider or installed UI acceptance claimed.

## NRA evidence and exact invocation

`nra-command.sh` is the exact successful full-context invocation: seven changed production targets, `--context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop`, under shell `timeout 165`, using `/home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python`.

`nra-before.json`, `nra-final.json`, `nra-rebased.json`: all `exact_compact_global`, **79 detectors, zero omitted, complete**. Payload mode is **counts_only**, so `findings: []` is not zero findings. Before: two findings (external enum case recovery + exact tiny method role). Final and main171 rebase: one pre-existing `exact_tiny_method_role` finding; external enum recovery is gone. This is structural analysis plus executed tests, not native-equivalence certification of authored patches.

## Files, cleanup, remaining scope

`changed-files.txt` lists every changed production/test path (eight production files including the new owner module). Evidence is under this directory. All owned test processes finished; removed owned `.artifacts/thread-status` (46 MB), local pytest/bytecode caches, and retained the successful and compressed failed evidence. No live or another worker's artifacts were removed.

Parent remaining work: apply paired Toad patch, perform its current UI/installation acceptance and deploy serially. Relationship169 was already merged through main170/171; its migration remains parent-owned. Darwin's history/index and ACP ownership was left untouched.
