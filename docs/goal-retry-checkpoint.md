# CLI goal controls and configuration checkpoint

Owner: core-goal-retry. Reviewer: codex-shepherd. Stop at draft review.

## Retry: eight source-backed findings

1. CLI start launches the registered worker, not a separate goal service: `src/agent_comms/owner_lifecycle.py:410`.
2. The client encodes `retry_goal` through the runtime family: `src/agent_comms/runtime.py:371`.
3. The receiving socket server decodes and applies that request: `src/agent_comms/runtime.py:248-251`.
4. Binding refuses a process other than the registered owner: `src/agent_comms/runtime_requests.py:73`.
5. Retry calls that owner's scheduler: `src/agent_comms/runtime_requests.py:332`; its ledger path is shared bus-root storage, `src/agent_comms/goal_scheduler.py:142`.
6. Goal creation only creates a ledger row when supplied a store: `src/agent_comms/goal_actions.py:415-416`; the CLI-invoked goal tool supplies none, `src/agent_comms/tools.py:449`.
7. Otherwise ledger creation waits for an exact streamed tool name: `src/agent_comms/turn_goal_account.py:153-161`; invocation through bash has no `comms_set_goal` tool-end event. The isolated invocation reproduces a missing row and the exact Retry refusal at `src/agent_comms/goal_actions.py:483-485`.
8. Therefore the reproduced gap is publication without a launch row, not a UI-private database. Goal creation must own the row before publication; turn accounting must consume that existing row. This does not yet establish why the reported live worker has missing rows or why every pending agent message is withheld. Those remain open, not attributed to a guessed common cause. The slow cursor display was explicitly withdrawn as evidence.

## Thinking level: eight source-backed findings

1. Previously Configure decoded the global level vocabulary and immediately published: baseline `src/agent_comms/cli_commands.py:748-749` (before this draft).
2. Vocabulary membership does not establish model support: `src/agent_comms/pi_vocabulary.py:140-195`.
3. The model-specific choices are owned by `ThinkingLevelConfigOption.discover`: `src/agent_comms/config_options.py:185-191`.
4. Configuration publication checks its selected value against those choices: `src/agent_comms/config_options.py:89-101`.
5. The drain catches that later failure after configuration sync: `src/agent_comms/input_drain.py:352-378`; `src/agent_comms/activity.py:75` renders it as Inbox unavailable.
6. This draft gives `CatalogConfigOption.require_value` one membership check and valid-choice refusal, used by describe, change and CLI validation: `src/agent_comms/config_options.py:126-134`.
7. Configure validates the proposed model and retained-or-requested thinking value before any registry change: `src/agent_comms/cli_commands.py:753-766`.
8. `NativeArguments.configured` owns environment/default argument selection for both Configure and TurnRunner: `src/agent_comms/native_arguments.py:154-166`, `src/agent_comms/turn_runner.py:105`; the runner's copied default and selection algorithm are deleted.

## Fork: retained source findings, not a completed repair

- `src/agent_comms/thread_management.py:577-590` asks the backend for a fork before declaring the child.
- `src/agent_comms/pi_native_backend.py:327-337` delegates to an external native helper.
- `src/agent_comms/native_fork.py:57-68` runs that helper against the parent's saved file.
- `src/agent_comms/_pi_helpers/fork_session.mjs:6` calls `SessionManager.forkFrom` in that separate process.
- `stack/native-session-storage.patch:1152` requires the parent's native writer lock before copying.
- `stack/native-session-storage.patch:285-286` refuses lock contention before entering the write body.
- This is a source-backed collision with an existing parent writer, not permission to remove a lock, retry an uncertain fork, or copy outside the native snapshot owner.
- The retained native manager's snapshot/fork lifecycle is the next investigation boundary. The specific running-parent failure has not yet been reproduced in this checkout, and no fork behavior is changed by this draft.

## Checks

- Before editing: isolated CLI-invoked `CommsSetGoalTool` published a goal with `store.snapshot(goal.id) == None`; Retry emitted exactly `Goal launch authority is missing; Retry cannot create a grant.` No live goal was used or changed.
- Before editing: Configure accepted `max` for a `fixture/no-reasoning` model without catalog discovery.
- After implementation: the two refusal regressions run against the original Configure method extracted from HEAD both fail with DID NOT RAISE. The changed implementation passes all five tests in `tests/test_cli_thinking.py`.
- Actual CLI/native path: the worktree's `python -m agent_comms.cli configure` on an isolated private bus queried the configured native catalog. For `openrouter/z-ai/glm-5.3-flash`, choices were `low, high, max`. `off` exited 1 and listed these choices, leaving the thread unchanged. `low` exited 0 and was stored. Metadata queries only, no model prompt.
- A first acceptance-script assertion incorrectly expected CLI error JSON on stderr; the application correctly wrote it on stdout. The corrected check above passed.
- Additional sanity run: `tests/test_native_arguments.py` had one existing failure: its one-shot regex expects `one-shot` but the current SelectedSession refusal says `Managed Pi session selection belongs to SelectedSession`. One other native-arguments case skipped because its native bundle environment was not supplied. The override follow-up below corrects the declaration-based fixture expectation and verifies that specific check; the skipped native journey remains outside this checkpoint's acceptance.
- Every check pinned to `taskset -c 4-9 nice -n 10`; no full suite or parallel native launch. Resource check warned of swap usage and 10.9 GiB free on /home, so checks remained small and sequential.

## Source evidence and limits

- Refactor-audit census and overlay parsed the production root before editing. Its Package loader enumerated all 306 Python modules, all class declarations/bases and relevant imports, writes, checks and calls. Zero parse omissions. Dynamic attribute binding is not resolved by this census. JavaScript/native dependency sites above were read semantically, not parsed by the Python scanner.
- NRA ModuleSyntaxIndex import was unavailable with this runtime's metaclass-registry dependency: DuplicateRegistryKeyError for ClassLineWitnessCandidate. Used the existing refactor-audit Package parser instead; did not build another parser.
- Before: CLI vocabulary membership and session catalog membership competed; default launch arguments lived in TurnRunner. After: CatalogConfigOption owns catalog membership/refusal for all three affected consumers; NativeArguments owns argument selection. No new family, cache or codec.
- Installation state: not merged or installed. Tests import source from this isolated checkout and borrow unchanged dependencies from the current runtime. No pip install/uninstall or edits occurred inside product runtimes; nothing was relinked.

## Override follow-up (2026-10-13)

- Cause: `src/agent_comms/config_options.py:182-191` let
  `AGENT_COMMS_AGENT_MODELS` replace model-specific thinking support with the
  global vocabulary. The model-list override belongs to `ModelConfigOption`;
  `ThinkingLevelConfigOption.discover` now always queries the native model's
  catalog. No second validation implementation or policy switch remains.
- AST census before deletion: 306 production modules, zero parse omissions;
  one thinking option declaration, CLI constructor at `cli_commands.py:761`,
  and declaration-derived session consumers through `ConfigOptions.catalog_for`.
- Three override regressions fail against the original `discover` method
  extracted from `dfec73363` (DID NOT RAISE); all eight thinking checks plus the
  declaration-based RPC-argument check pass (nine total, 0.22 seconds).
- The RPC fixture now derives the refusal from the class's own C3-selected
  `validate_rpc` method rather than assuming every one-shot capability overrides
  session selection. Absent short flags are not passed as `None` tokens.
- Actual source CLI + configured installed native catalog on an isolated private
  root, with the model-list override set: GLM `off` exited 1, listed `low, high,
  max`, and preserved the thread; `low` exited 0 and persisted. No provider input.
  Two initial fixture setup checks were refused before configuration (missing
  private marker, then inherited live root ID); the final distinct root used the
  initializer and removed inherited owner routing variables. No live mutation.
- The small sequential run reused installed dependencies under critical host
  pressure. Owned disposable output:
  `/home/ts/.cache/agent-scratch/core-goal-retry/thinking-override-check`;
  retired after checks and borrower inspection. No uncertain inputs there.
- This follow-up remains draft, unmerged and uninstalled. It does not claim the
  skipped saved-native-selection journey or the blocked response resolver passed.

## Retention

- Owned source/test environment: `/home/ts/wt/core-goal-retry/.venv`, needed for this continuing piece. Only pytest and pytest-asyncio plus their dependencies installed there.
- Disposable source-audit and isolated verification output: `/home/ts/.cache/agent-scratch/core-goal-retry`, retired after recording these results. No user sessions, auth, wire data or UNKNOWN inputs were placed there.
- Retry and fork remain owned by core-goal-retry. This is a working configuration checkpoint, not closure of the complete assignment. No blocked live goals were cleared or retried.
