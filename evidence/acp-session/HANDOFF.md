# S7 ACP session lifecycle, configuration and transcript boundary

Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/149
Worktree: `/home/ts/wt/comms-refactor-s7-acp-session-20260928`
Branch: `codex/refactor-s7-acp-session-20260928`
Original base: PR145 `6b111f7`; rebased onto main `2f01780`.
That main includes the parent's PR147 normal FULL coding entrypoint correction.
No competing edit to that entrypoint or its SelectedToolIntent policy.
Final production commit: `46a383a` (the following commit only records evidence).

## Completed ownership and consumers

- `session_lifecycle.py`: SessionLifecycle owns bindings, client attachment,
  negotiated transcript replay, proxy connections/image capabilities, published
  title/worktree caches, initialize/new/load/attach/identity sync, session
  metadata and owner release. AttachedSessionLifecycle inherits common behavior
  and owns stdio attachment/new/load without claiming execution authority.
- `config_options.py`: ConfigOptions owns model/thinking discovery caches,
  locks, generations, per-session published signatures and the existing
  PendingRequests correlation store. Model/ThinkingLevel ConfigOption classes
  own rendering and mutation; membership, dispatch and change detection all
  derive from the same A1 declarations. S2 SetModel/SetThinkingLevel own the Pi
  command shape. Backend observation, external-change polling and auth refresh
  consume this owner. Thinking fallback/normalization has one implementation.
- `transcript_updates.py`: the legacy dictionary boundary uses A1/A2; typed
  Comms TranscriptEvent projections are trusted without serializing/decoding
  again. User/assistant/notice/sent/started publication belongs to declarations.
  Unknown legacy kinds retain their prior silent behavior. Snapshot/diff clients
  retain the existing full TranscriptPage schema and native input-display rules.
- `session_effects.py`: explicit public ABC for the remaining delivery/turn
  effects. It exposes operations, not another copy of session state. Queue and
  private native-cursor metadata remain with their existing authoritative owner.
- `acp.py`: composition root and ACP facade. Old implementations and session/
  configuration field allocations are removed. Remaining turn consumers use
  `sessions`/`config` directly. Runtime/compaction private ABI properties refer
  to the one component-owned state; they do not mirror it. Typed S1/S2/S8 event
  consumers remain intact. Existing saved-thread aliases/working-directory
  checks, owner reuse, advisory initialization and UNKNOWN preservation remain.
- `tests/test_session_components.py`: adding one option reaches actual ACP
  router discovery/set-option/persistence AND external-change notification;
  adding one transcript update reaches the existing consumer without changing
  a roster. Tests also prove separate attachment preferences, single state
  ownership and exact legacy ACP JSON publication.
- `tests/test_acp.py`: its configuration stub now patches `agent.config.options`,
  where the implementation actually lives.

The remaining CommsAgent turn/input scheduling and native cursor/admission
implementations are not this slice. PR95's enabled-by-default constructor and
compaction policy, native/coordination/claims internals, runtime/CLI commands,
packaging and deployment were not changed.

## Verification at its actual strength

- `tests-acceptance.txt`: **242 passed, 15 skipped in 73.00 seconds**, after the
  main/PR147 rebase. Includes actual ACP stdio/router tests, real Unix owner
  sockets, new/load/reconnect/rename/project behavior, auth/configuration,
  saved transcript/input routing, goals, queue contracts, private delivery and
  PR95 compaction interfaces. Bound: 165 seconds, xdist disabled.
- Subsequent declaration closure: configuration change signatures now derive
  from every ConfigOption, and typed saved projections bypass redundant JSON
  decoding. `tests-catalog.txt`: **108 passed in 24.29 seconds**, covering the
  final configuration behavior plus ACP/auth/project paths. The final replay
  and socket selection is in `tests-final-boundaries.txt`: **33 passed in 3.54 seconds**.
- Fifteen optional native cases need PI_COMPACTION_TEST_PACKAGE and were skipped;
  this does not claim native/provider PR95 retention acceptance. Parent owns it.
- `tests-integration.txt` also records five selected-write failures, all
  `Selected owner has no configured provider/model`. The original pre-refactor
  ACP module was loaded against the same dependencies and tests; the exact same
  five fail and three pass (`tests-selected-write-baseline.txt`). Those fixtures
  manually register unconfigured owners and bypass normal session setup. No
  production coordination/model policy was changed to accommodate them. They
  are outside the passing combined selection, not silently reported green.
- First new-case test assumed the SDK router returned an object for setting
  changes; its actual contract is a JSON dictionary. The assertion was fixed;
  the earlier failure remains in `tests-components.txt`.
- Ruff and diff whitespace checks pass. Fresh contextual NRA scans select all
  five changed production modules with `--context-root src`, two parse/analysis
  workers and a 45-second internal budget inside a 60-second bound. The final
  result is `nra-final.json`: **exact_compact_global, 79 analyzed, 0 omitted,
  complete=true, 0 findings on the selected changed files**.

This is authored component/family synthesis following the S7 ownership decision,
not a claim of NRA-native semantic equivalence proof. The existing operation
catalog does not choose or synthesize this lifecycle/effect boundary. Behavioral
proof is the executed tests. NRA reports scoped findings with complete package
context, not global cleanliness of all remaining S7 surfaces.

## Repeat local verification

Test interpreter:
`/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python`.
It supplies pytest against the existing runtime dependencies; `PYTHONPATH=src`
selects this worktree. Create `.test-artifacts` before using persistent basetemp.

```sh
PYTHONPATH=src timeout 165 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python -m pytest -o addopts='' tests/test_session_components.py tests/test_acp.py tests/test_auth.py tests/test_project.py tests/test_runtime.py tests/test_runtime_goal_edit.py tests/test_runtime_goal_snapshot.py tests/test_runtime_goal_retry_running.py tests/test_acp_input_disposition.py tests/test_transcript_input_display.py tests/test_tool_diffs.py tests/test_acp_goal_input_authority.py tests/test_acp_compact_command.py tests/test_acp_compaction_activity.py tests/test_acp_queue_contract.py tests/test_acp_private_nk_delivery.py tests/test_owner_compaction_adaptive.py tests/test_owner_compaction_settings.py tests/test_compaction_publication.py tests/test_owner_compaction_gate.py --basetemp=.test-artifacts/acceptance -q
```

## Parent integration

Review/merge/install through the existing procedure. New components are imported
by normal ACP entrypoints; no flags or data migration are needed. Existing
processes keep their loaded version until the parent's normal safe refresh.
Reversing the code installation needs no saved bus/session/registry restoration.
No owner restarts, live roots, provider calls or model overrides were performed.
CI remains deferred. Disposable local test/NRA artifacts are cleaned after their
processes finish; tracked evidence and the persistent worktree remain.
