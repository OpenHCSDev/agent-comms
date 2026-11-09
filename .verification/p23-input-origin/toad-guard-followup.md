# Toad guard follow-up

Assembly: Core `7b4c03223b716a5625e358b6b297169d378b12ae`, Toad `6587aee50bc73af398d16a33c6807c33bdeec038`.
Request: #comms 12083 (`c0fe427d9e05`). NRA source: `5981d592bd5297342ad8f1f129e6bfdd213906f0`. This is an artifact-only response; neither shared checkout was edited.

## P23 probes: native whole-script placement is still owed

Requested moves, with bytes and execution order preserved:

- `tests/native/command_target_probe.py` → `tests/support/command_target_probe.py`.
- `tests/native/goal_read_probe.py` → `tests/support/goal_read_probe.py`.

`tests/guards/test_test_layers.py:79` admits non-test Python helpers only under support. Do not rename the scripts to collected tests, delete their checks, add a synthetic declaration, move their cold import later, or keep an old forwarding script.

The original scripts first execute `importlib.import_module(sys.argv[1])` (lines 7 and 8 respectively), then import and inspect the actual production classes. Preserve the argv contract, real imports, complete assertions, JSON output and fresh-subprocess isolation. `command_target_probe` also reports the actual MainScreen, Conversation and GoalBar MROs and determining property owners. `goal_read_probe` resolves the original GoalSession annotations and checks the original capabilities.

The native declaration selection refuses both original scripts. Exact output and original source hashes are in `probe-placement-native-selection.json`; replay the bounded selection check with `check_probe_placement.py` using the NRA source checkout on PYTHONPATH. This ran native selection only, not a test, probe execution, snapshot simulation or product edit.

The deciding implementation is `nominal_refactor_advisor/codemod_module_declarations.py`: `SourceTopLevelDeclarationIndex.carrying` carries assertions and bare calls only **after** a selected declaration. `WholeModuleSymbolMoveSelection.of` rejects the original cold-import calls preceding any declaration. `goal_read_probe` has no movable declaration. `dissolve_module` deletes only an emptied module, so it cannot independently move these original executable bodies.

NRA's owner needs a whole-script placement operation which preserves the complete ordered module body and its original import boundary, removes the original file, and migrates actual path/module consumers. The exact source refusal was sent to comms-friction and Cloud in message `a6fe727c5dcb`. No engine change or proof bypass was attempted here. **There is no usable native probe-move plan yet.**

### Caller findings

At the pinned Toad head, a whole-tree `git grep` for both probe basenames finds no tracked caller. The same search in Core finds no executable source caller; retained simulation reports mention their indexed paths. These are manually invoked subprocess scripts, not collected tests. Friction's future invocation must change only the script argument to the new support path, retaining the original interpreter, environment, working directory, first-module argument, timeout and output handling. This report does not claim to have recovered an unrecorded historical invocation or executed either script.

## Separate unchanged-main repair: app-layer test

`channel-display-layer-placement.plan.json` relocates the original decorated `test_shown_messages_are_read_exactly_and_hidden_expansion_stays_unread` from `tests/app/test_channel_display_basis.py` to `tests/textual/test_channel_display_basis.py`, then dissolves the emptied original module. Its actual dependency is `PaintedApp.has_shown`, reached through `ui.textual` inside `shown()` at line 31. Keep the five heights, actual app, ledger and hidden-expansion assertions unchanged. This is placement, not a weakened app-layer guard.

Native payload decode passed. No native simulation, application, test collection or runtime check was run. The recorded documentation path in `docs/agents/TESTS.md:216` must follow the move; the native successor proof owns that text migration. The native apply must include that documentation surface rather than silently omit it.

## Separate unchanged-main repair: bus-choice keys

`bus-choice-declared-action.plan.json` declares `ForgetSavedBusAction(WidgetAction)` in the existing bus-choice module and attaches it through `BusChoices`' `actions` keyword. It removes the raw Binding at line 195 and handwritten `action_forget` at line 203, retaining `ListsBuses.forget_highlighted` unchanged. The required key, label and visible hint have one declaration. This is IMPL-1/MEMB-2: the declared Action family owns key behavior; paper1 Theorems 4.6 and 4.17 separate supply from precedence.

**Priority matters:** the existing raw Delete Binding defaults to priority false, while PriorityActions promotes declared actions. The plan restricts its scope to the existing DismissAction so the new Delete action is not promoted above a focused address editor. The base list and actual deletion procedure remain unchanged. Whole-tree source search found no other `action_forget` or `"forget"` caller in Toad tests; only the removed binding and method used those spellings.

Native payload decode passed. No simulation, module execution, key-dispatch check or UI test was run. This plan is a separate proposed guard repair, not P23 qualification.

## Evidence and next owner

`guard-plan-decode.json` records the native decoding of both separate repair plans. `guard-source-check.json` records three unique bus-choice patch hunks, successful AST parsing, unchanged ListsBuses procedure and BusChoices bases, and native whole-module selection of the original decorated channel-display test. Native selection refusal is distinct from an unsupported CLI invocation: it called the actual declaration-selection implementation on the original pinned script bodies and retained its ValueErrors.

Friction owns applying usable plans to the combined assembly and qualifying the resulting consumers. Cloud/NRA owns adding or identifying the missing whole-script operation. Carry diagnosis stays with comms-teammate and native goal-bar failures with codex-latency. This artifact does not approve merge, install or the real Standby-owner acceptance.
