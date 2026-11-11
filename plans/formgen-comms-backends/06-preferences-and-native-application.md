# 6. Preferences and native application composition

**Planning status:** the existing settings store, durable readers, effect dependencies and entry-point split below are traced. Exact pure choice/native presentation bases, the common CLI construction and final app MRO still need completion. No Qt application or lightweight Qt-only distribution is claimed by this document.

## Outcome

Both applications edit the original declared settings through one ObjectState and the original save/discard algorithm. Native effects apply through the application's actual controls and layout. Qt does not create a second JSON schema, settings value cache, connection application or command parser.

Use the shared owners from phases 1–5. Adding an interface over two full native applications does not meet this outcome.

## 6.1 Retain the settings model and save algorithm

Original owner: `toad/preferences.py:340–610`, particularly `SavedSettings(ExternalStore)` at `:414`. Proposed shared destination: `toad/client/preferences.py`. Move the existing store and typed settings; remove the former binding once all callers and the writer-owned-store entry point use the new module.

Keep these state distinctions and original methods:

| Original method/state | Required behavior |
|---|---|
| `root`, `state`, `current` | One FieldPath/ObjectState/typed settings tree; forms edit this same state |
| `in_file`, `user_set`, `keys_held`, `saved_keys` | Preserve which keys were present or explicitly set, independently of whether their values equal defaults |
| `set`, `toggle`, `changed` | Validate through the bound DeclaredField; update original provenance and apply/publish the change |
| `discard` | Restore the saved ObjectState and its original explicit-key set |
| `kept`, `document`, `json` | Write only retained settings, preserve explicitly empty groups, use original codec wire names, reject non-JSON numeric values |
| `open_edits`, `save_lock`, `save` | Finish pending native edits before locking/collecting the document; serialize writes; run atomic file work off the UI loop |
| `written(text)` | Mark saved only when the current document still equals the written text; edits made while writing remain unsaved |
| `save_before_exit` | Exit only after successful save leaves no unsaved settings |
| `practice_opening`, `path`, `ensure_file`, `ensure_installation` | Preserve real installed-file reading, location, creation and anonymous-installation identity |

`SavedSettings.set` explicitly retains a user-set value even when it equals its default. It is wrong to rebuild the file by comparing values with defaults. The saved empty `diff` group is also observable and must survive.

The existing `changed` scans declared leaf paths; this plan does not call that algorithm a new performance result or introduce another mirror of setting values. Any indexing improvement belongs at the original state/path owner with its own preservation proof.

The current constructor and `changed` contain function-local event imports. Their current presence is not permission to move them unchanged. Use the phase-2 event split and NRA's complete import graph so the shared store imports its event declarations normally. No new guarded/local import lands.

## 6.2 The declarations are not yet native-free

A dataclass field does not make its dependencies pure. Current `preferences.py:25–60` imports native prompt effects, TerminalLayout, presentation budget, key modes, DiffSettings and native choice families. `setting_declarations.py` also annotates effects with ToadApp, and `Effect.open_widgets` walks native view trees.

Resolve these dependencies before importing the shared store:

- `FamilyChoices` retains the original family-members/choice-label implementation. Its Core `DeclaredChoice` dependency is allowed in the shared comms client, not a reason to add Core to Formbridge.
- Shared `Effect` takes the shared preference host and `DeclaredField`, not ToadApp. The original metadata remains the declaration of which effect applies; do not create a second path-to-effect registry.
- `EachOpenWidget` currently derives its owner from the declaring class in `__set_name__`. Preserve that capability membership. Native open-target enumeration must come from the actual workspace/session owner; Qt must not keep another registry of every open form/prompt.
- `PromptSettingEffects` currently contains Textual tree walking and `refresh(layout=True)`. Those native operations remain native, while shared input settings and their effect selection retain one declaration.
- `InputBox` currently declares Textual `Spacing`, `Color` and `Edges` behavior; `Scrollbar.size` is in terminal cells; `NavigationBarPosition.place` moves Textual widgets; `LoadingStyle.widget` constructs a Textual loading widget. They cannot enter `toad.client` unchanged.
- Keep the distinct declared choices and their stored names. Split native presentation from choice identity/meaning rather than replacing these families with strings or merging choices that currently look similar.
- Walk `DiffSettings`, key modes, layout, themes, notification choices and their annotations before claiming the root imports without a renderer. A fake import test that preloads Textual would miss this problem.

**Remaining declaration work:** record the exact original pure/native class split for each of those families, native method winners and the shared preference-host signatures. This is a required part of P144, not a new task for an implementer to guess.

## 6.3 Effects to preserve

These are the actual `setting_effects.py` declarations, not an invented list of settings:

| Effect | Original behavior | Required native split |
|---|---|---|
| `visibility` | Update widgets composing SettingVisibility and refresh prompt layout | Native target lookup, display and layout; do not probe arbitrary widget attributes |
| `wheel_scroll_rows` | Set native vertical scroll sensitivity | Preserve terminal-row meaning for Textual; do not reinterpret the stored number as Qt pixels |
| `history_buffer_viewports` | Update each actual HistoryWindow's presentation budget and request preparation | Native windows/layout measure their own viewport; retain source preparation rather than rebuilding the history |
| `conversation_layout` | Call the existing FollowsLayoutPreferences capability on open targets | Native layout implementation; one declared capability, no backend-kind branches |
| `sidebar_filters` | Update Core observation interest | Shared connection/interest behavior from phase 3 |
| `theme` | Select through the original colour owner | Native appearance selection; preserve an unknown saved theme name rather than deleting it |
| `key_mode` | Apply the prompt keyboard profile and refresh bindings | Native input/binding operations, original key-mode meaning |
| `sessions_bar` | Update the original session-bar policy | Native tab chrome over phase-4 session membership |
| `blink_title` | Update terminal attention | Native attention/title behavior; it is not a new Core notification state |

The original prompt input layout/text effects are also in scope. An effect with no applicable native surface must have a declared, visible policy; do not silently discard its stored value or treat an unsupported effect as evidence that the setting is unnecessary.

Core still owns permissions and observation interest. A settings form cannot grant a command merely by displaying its control, and a Qt effect cannot choose a new source because a delayed callback saw a replacement widget.

## 6.4 Durable readers and caller changes

`pyproject.toml` currently declares:

- Script `toad = "toad.cli:main"`.
- Writer-owned-store entry points `toad = "toad.db"` and `preferences = "toad.preferences"`.
- Mandatory Textual, image, diff and web-server dependencies; Python `>=3.14,<3.15`.

When the store moves, change the preference entry point to its real shared module in the same commit. Check installation discovery and `SavedSettings.practice_opening` against an actual copied saved file. The public stored keys and reader behavior stay unchanged; changing Python ownership does not require a format migration or compatibility alias.

Current production imports naming preferences/declarations/effects are in `application_actions.py`, `setting_widget.py`, `sidebar_layout.py`, `textual_colours.py`, `cli.py`, `widgets/conversation.py`, `widgets/session_sort.py`, `patch.py`, `about.py`, `widgets/side_bar.py`, `keymap.py`, `screens/keymap.py`, `app.py`, `setting_effects.py` and `preferences.py` itself. The search also found `tools/physical_ui.py`. Move shared imports and keep native effects at native owners; do not keep old-path re-exports.

Test consumers: `tests/model/test_saved_settings.py`; Textual `test_settings_screen.py`, `test_forms.py`, `test_sidebar_drag_resize.py`, `test_history_runway.py`, `test_application_actions_in_the_running_app.py`, `test_selection_across_clipped_diff.py`, `test_navigation_keys.py`, `test_keymap_model.py`, `test_scroll_down_bounce.py`, `test_sidebar_layout_policy.py`, `test_active_mode_keymap.py`, `test_prompt_enter_insert.py`, `test_lazy_tool_output.py`, `test_navigation_bar_position.py`, `test_keymap_screen.py`, `test_conversation_column_alignment.py`, `test_offscreen_tool_diff.py`, `test_transcript_presentation_fencing.py`, `test_tab_close.py`.

Finish the import-graph/dynamic-entry check before calling this the final complete caller graph. The original saved-settings fixture is a real durable contract, not a golden file invented to freeze an internal implementation.

## 6.5 One startup path, native entries

Current `cli.py` directly imports ToadApp and ToadWebServer. `DefaultCommandGroup.parse_args` selects the Core connection once at the process boundary; `run` and `acp` construct ToadApp; `run_terminal` owns Textual's escaping/retained failures and calls the returned outcome's `finish`.

Keep connection selection, checkout decoding, agent binding, original launch identity, session decoding and argument meaning in one shared startup path. Native terminal failure persistence and Textual web serving remain native. The Qt entry imports Qt, constructs its native application over those same shared owners and runs the persistent UI-loop lifetime from phase 2.

A proposed Qt script is `toad-qt = "toad.qt.cli:main"`; preserve the ordinary `toad` entry. It must select the native entry through the entry declaration, not an environment variable, `if backend == ...` in the application or two copied Click command groups. The exact shared Click group/context construction and native app constructor signatures still need to be recorded from the complete CLI and app source.

The final native application base lists must compose the shared host, connection, command, navigation, history, form and preference owners with their own framework base. List constructor order and winners for setup, publication, view selection, save and close. A class list or a simulated MRO does not execute either framework's metaclass.

Package optional-backend declarations, installer selection and Python-3.14 Qt-loop support are still unresolved. Do not claim a Qt-only install without Textual while Textual is a mandatory dependency. Do not add an undeclared package to an interpreter to make an import check pass. The existing installation/carry owners must verify that package change before live installation.

Provider login presentation is also unfinished: trace the original provider request/result and browser/input flow before assigning native dialogs. No second login authority or unapproved paid-provider test belongs here.

## 6.6 Ordered work and verification

1. Complete the native-dependent choice/effect/host and CLI class map above. Keep each stored identity and original algorithm; name any required answer a proposed merge would lose (paper1 Theorem 4.8; paper4 Theorem 2.6).
2. Use NRA binding-removing relocation for pure declarations, and member promotion only where its existing-ancestor proof applies. The completed form scan provides no automatic recipe for these preference moves. Unsupported native descriptor/metaclass/import moves are explicit NRA gaps, not a hand-written codemod exemption.
3. Move the original store and event/effect algorithms once, migrate every consumer and the writer-owned-store entry point, and delete the old bindings. Wire each native application through its shared parents.
4. Preserve original model/settings tests, and add only new native boundary cases for input completion, effects, file-write failure and application close. Do not port tests whose only assertion keeps a deleted alias alive.
5. Run native-free shared import discovery in a fresh interpreter, all changed-name consumers and actual native constructor/MRO checks. Preserve the real saved document on read/write and verify installed store discovery on a scratch copy before any live carry.
6. Run both native settings/command/history/tab flows with a real Core connection. Qt must close in the phase-2 order and preserve pending edits/write failures, stopped interests, source retirement and child destruction. This planning task does not run or claim that application.

Required existing tests include `test_saved_settings_write_back_unchanged`, `test_the_file_holds_what_the_user_has_set_even_a_value_equal_to_its_default`, `test_a_group_the_file_holds_empty_is_written_back_empty`, numeric-bound/non-JSON-number refusal, and all settings-screen tests. In particular keep `test_closing_the_form_sends_the_number_still_being_typed` and `test_a_newly_declared_setting_is_edited_applied_saved_and_read_back`. Keep `test_app_close_order.py` and its actual acquired-history retirement controls.

Add native controls for a refused focused edit during Save, a valid focused edit during Close, atomic write failure, edits arriving while a write runs, discard after explicit-default edits, a hidden/parked prompt following its setting, unknown saved theme, view destruction during an effect and restart reading exactly the stored keys. Use the real store/form owners rather than writing private state.

**Ready/done:** all remaining class/CLI/runtime decisions are supplied; both applications execute the same original settings/startup behavior; native imports stay out of shared modules; the original stored file remains readable; old bodies and entry bindings are removed; actual native runs establish the required effects and shutdown. P144 stays unfinished until those decisions have stopped being architecture homework.
