# 6. Preferences and native application composition

**Planning status:** select the original concrete settings schema, one shared store/effect algorithm, a common Click startup factory, native application construction and the existing PTY/ANSI owner for interactive login. The initial Qt extra is added to the existing full Toad distribution; a Qt-only installation without Textual is not this release target. Native execution remains a required implementation check, not evidence claimed here.

## Outcome

Both applications edit the original declared settings through one ObjectState and the original save/discard algorithm. Native effects apply through the application's actual controls and layout. Qt does not create a second JSON schema, settings value cache, connection application or command parser.

Use the shared owners from phases 1–5. Adding an interface over two full native applications does not meet this outcome.

## 6.1 Retain the settings model and save algorithm

Original owner: `toad/preferences.py:340–610`, particularly `SavedSettings(ExternalStore)` at `:414`. Extract its original storage/state/edit/save bodies to `toad/client/preferences.py: SettingsState`. The existing `toad.preferences.SavedSettings` becomes the concrete declaration `SavedSettings(SettingsState, schema=ToadSettings)` with no copied methods. Both native entries use this same schema/store declaration and the same file. Shared client algorithms accept SettingsState and never import the concrete schema to pick a renderer.

This deliberately retains the concrete schema at its original owner. It currently contains native keyboard-catalog validation and editing-mode classes, not merely JSON data. Moving that module unchanged under client would make the native-free claim false; deleting its validator to force a pure import would change the admitted saved files. The initial full-stack package retains that dependency at the native entry boundary. Generic store code imports neither it nor a native toolkit. The writer-owned-store entry point stays `preferences = toad.preferences`, because it still owns the real concrete reader, not an alias.

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
| `practice_opening`, `path`, `ensure_installation`; app `ensure_settings_file` | Preserve real installed-file reading, location, creation and anonymous-installation identity. Current main already moved file-creation UI feedback into ToadApp; do not reintroduce SavedSettings.ensure_file. |

`SavedSettings.set` explicitly retains a user-set value even when it equals its default. It is wrong to rebuild the file by comparing values with defaults. The saved empty `diff` group is also observable and must survive.

The existing `changed` scans declared leaf paths; this plan does not call that algorithm a new performance result or introduce another mirror of setting values. Any indexing improvement belongs at the original state/path owner with its own preservation proof.

Current Toad 1421d01d4 already removed SavedSettings.open/ensure_file and its guarded ToadApp import: ToadApp.open_settings/ensure_settings_file own native callbacks and file-creation feedback. Retain that repair rather than move stale application methods back into the store. The constructor and changed still contain function-local event imports; those do not move unchanged. Use the phase-2 event split and NRA's complete import graph so the shared store imports its event declarations normally. No new guarded/local import lands.

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

### Selected declaration and native contracts

Keep each original choice identity and stored spelling. Move only independently pure declarations to `toad/client/setting_choices.py`: SchemeName; Expansion/Never/Always/Success/Fail/Both and their original ToolCallStatus decisions; UnreadCount/Shown/Hidden; SessionBar/Always/Multiple/Never; Scrollbar/Normal/Thin/Hidden; and the pure NotificationPolicy.enabled members. NotificationPolicy's desktop resource, Lock, platform notification construction and send stay at the native platform boundary, called only after the original enabled(focused) decision. Do not create another enabled roster.

The concrete schema remains in toad.preferences and imports those identities directly. Keep InputBox's original native spacing/border presentation, LoadingStyle's Screen/FutureText constructors and KeyMode's EditingMode/NeovimEditing behaviors native. Their stored choices are still one declaration, and both forms still display/edit those same choices. Native Qt effects translate the selected original presentation at the boundary, without duplicating its values or importing its native types into client. In particular, apply existing InputBox spacing through Qt font metrics once; do not copy the Normal/Compact/Flush constants into a Qt table. NavigationBarPosition.around is generalized to a type-preserving pair of native objects without changing its Top/Bottom bodies; its Textual move_child implementation stays native, and Qt places that returned pair in its actual layout. Scrollbar.size remains terminal-cell preference data; Qt measures the intended width with its own font/style rather than calling it a history row distance. LoadingStyle selection calls a native LoadingHost primitive: Pulse requests the native loading control; Quotes keeps its original quote selection once and asks the native host to display that text. No QLabel stands in for an interactive input control.

Keep Keymap's actual native catalog validation at the concrete settings boundary: KeyInvocationSpelling.from_text currently calls KeymapCatalog.capture, which discovers DOMNode contexts and ApplicationAction declarations. It is **not** a pure string record and must not be moved into client unchanged or weakened into unvalidated text. The Qt entry may load this original concrete schema because this release still installs Textual. Its native keymap editor uses the original captured invocation identities and validation; binding dispatch is projected to the shared command/host contract, not by invoking a Textual DOMNode against a Qt widget. Native prompt editing supplies a real Neovim host for the original Vim modes; no consumer compares saved mode names. If that native boundary is not implemented and tested, the parity phase does not ship—retaining a setting's JSON while silently ignoring it is not parity.

The required Neovim split is member-scoped, not a second editor engine. In `toad/client/neovim_session.py`, retain the existing NeovimHost/request/frame/reply/buffer-lease protocol and extract `NeovimBufferSession` from NeovimEditing's pending/outgoing/generation/snapshot/lease algorithms. The original process/requests/ready tasks, focus_requests, completion matching, accept_lines/accept_display, publish-before-render ordering, failure retirement and close/join remain one implementation. NeovimHost.ready starts its original _run through the phase-2 operation owner instead of application.run_worker. The shared host contract owns neovim/forget_neovim; native HostsNeovim no longer makes the shared protocol inherit Textual App.

Keep Detached/Retained/AttachedReceiver's TextArea/DocumentBase projection, Selection/CursorShape construction, Textual Binding dispatch, NeovimUpdated(Message), call_later/post_message and render/invalidate methods native. Add an editor-receiver contract at that existing boundary: initial lines/cursor/indentation, declared mapping entries, project line changes, finish projection, deliver the queued paint, apply an engine boundary and retain/detach the actual document. Textual receiver methods retain their original bodies; Qt receivers use the actual QTextDocument/selection/input owner. The shared session never treats a QTextEdit as a TextArea or stores a second copy of its text. Both native profiles configure that same session from the original KeyMode's initial_keys/map_modes; a Qt class does not register another VimInsert/VimNormal/VimVisual choice. Neovim's row/byte-column protocol is decoded at each native editor boundary, including Qt UTF-16 positions, Unicode and multiline selections.

Preserve the important source controls at neovim_editing.py: `_write_queued` receives the already-created start task, not a re-entry into its cached _started property; focus leaves the prior buffer/operator before entering the next; resize never advances input generation; a failed engine reopens from the last displayed text without replaying unfinished input; flush refuses incomplete work rather than waiting for deferred RPC in application dispatch. Qt prepare_input fences its receiver's accepted input queue, not the Neovim event loop, so Escape remains admissible. Native paint exposes a completed snapshot only after its preceding line batch has projected. These are required shared/native tests, not permission to emulate Vim with a small shortcut table.

Move DiffSettings' data/choice decisions only after following their returned PatchRows/DiffMarks values; the native Patch(LineBlock), renderer refresh and theme signal stay native. Preserve TerminalLayout/TerminalOrigin and SidebarPlacement/BandSplit as the one durable layout schema; TerminalOrigin.of stays the existing environment decoder, while Qt declares its actual placement origin explicitly rather than pretending it is an SSH terminal. PresentationBudget's scalar budget declaration can move shared; methods consuming Widget, Region, Screen or native costs stay with the native presentation owner. These are per-member moves, not a whole-module relocation based on a class name.

Add `PreferenceHost` in `toad/client/preference_host.py` with the exact operations effects already need: enumerate open targets of a declared capability; refresh prompt layout/text; apply wheel sensitivity and history budget; apply conversation layout/theme/key profile/session-bar visibility/title attention; and update sidebar interest through the original coordination access. Shared Effect.apply takes this host and DeclaredField. Move EachOpenWidget's declaring-owner capture and iteration unchanged; the native enumeration hook walks the existing workspace views/screens (Textual) or actual QObject/widget trees under the same session workspace (Qt). It does not keep a second open-widget registry. PromptSettingEffects' effect descriptors move onto a shared prompt-setting capability; its native widget lookup/refresh remains implemented by each actual prompt.

`SettingsState` takes the concrete class's declared schema, retaining applied/report_error callbacks and all original methods in §6.1. The concrete schema is supplied once by the subclass keyword, not restated by each form. Tests that deliberately pass a different root declare a corresponding SettingsState subclass; migrate those test call sites rather than preserve a second root-selection constructor API. The move changes Python ownership, not the one file's keys, explicit defaults, empty groups or validation.

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

Keep the concrete preference entry point at its real `toad.preferences` schema/reader declaration while extracting SettingsState. Check installation discovery and inherited `SavedSettings.practice_opening` against an actual copied saved file, including a nonempty keymap. The public stored keys and reader behavior stay unchanged; no format migration or compatibility alias is introduced. Do not redirect discovery to a generic base that cannot know which concrete schema validates that file.

Current production imports naming preferences/declarations/effects are in `application_actions.py`, `setting_widget.py`, `sidebar_layout.py`, `textual_colours.py`, `cli.py`, `widgets/conversation.py`, `widgets/session_sort.py`, `patch.py`, `about.py`, `widgets/side_bar.py`, `keymap.py`, `screens/keymap.py`, `app.py`, `setting_effects.py` and `preferences.py` itself. The search also found `tools/physical_ui.py`. Move shared imports and keep native effects at native owners; do not keep old-path re-exports.

Production construction is ToadApp.open_settings and ThemedApp's non-app default in textual_colours.py; both construct the same concrete SavedSettings declaration. The only located different-root construction is the ExtendedSettings settings-screen test; migrate that test to its explicit concrete subclass of SettingsState. Keep KeyInvocationSpelling/KeymapCatalog callers on the concrete native schema boundary and include the new current-main HintLabels setting through the ordinary declared-field family, not a Qt roster.

Test consumers: `tests/model/test_saved_settings.py`; Textual `test_settings_screen.py`, `test_forms.py`, `test_sidebar_drag_resize.py`, `test_history_runway.py`, `test_application_actions_in_the_running_app.py`, `test_selection_across_clipped_diff.py`, `test_navigation_keys.py`, `test_keymap_model.py`, `test_scroll_down_bounce.py`, `test_sidebar_layout_policy.py`, `test_active_mode_keymap.py`, `test_prompt_enter_insert.py`, `test_lazy_tool_output.py`, `test_navigation_bar_position.py`, `test_keymap_screen.py`, `test_conversation_column_alignment.py`, `test_offscreen_tool_diff.py`, `test_transcript_presentation_fencing.py`, `test_tab_close.py`.

Finish the import-graph/dynamic-entry check before calling this the final complete caller graph. The original saved-settings fixture is a real durable contract, not a golden file invented to freeze an internal implementation.

## 6.5 One startup path, native entries

Current `cli.py` directly imports ToadApp and ToadWebServer. `DefaultCommandGroup.parse_args` selects the Core connection once at the process boundary; `run` and `acp` construct ToadApp; `run_terminal` owns Textual's escaping/retained failures and calls the returned outcome's `finish`.

Keep connection selection, checkout decoding, agent binding, original launch identity, session decoding and argument meaning in one shared startup path. Native terminal failure persistence and Textual web serving remain native. The Qt entry imports Qt, constructs its native application over those same shared owners and runs the persistent UI-loop lifetime from phase 2.

A proposed Qt script is `toad-qt = "toad.qt.cli:main"`; preserve the ordinary `toad` entry. It must select the native entry through the entry declaration, not an environment variable, `if backend == ...` in the application or two copied Click command groups. Use the common startup and native construction contract below; the native entry class selects the renderer, never an environment variable or application branch.

The final native application base lists must compose the shared host, connection, command, navigation, history, form and preference owners with their own framework base. List constructor order and winners for setup, publication, view selection, save and close. A class list or a simulated MRO does not execute either framework's metaclass.

Keep the existing mandatory Textual dependencies for the initial dual-frontend distribution. Add a declared Qt extra containing the existing PyQtReactive package and corrected qasync dependency; `toad-qt` uses that extra, not a new distribution with a copied application. The installer resolves those packages through its existing tracked-main/package selection and verifies the complete resulting set before carry. Python-3.14 qasync owner fixes and actual Qt construction are the phase-2 implementation prerequisites. No hand-installed dependency, version negotiation or manual pin list substitutes for that build.

Provider login has three distinct existing paths, traced below. Their native terminal/picker targets and required checks are selected here; no implementation or provider-authentication result is claimed.

## 6.6 Login: retain the actual request and readiness owner

Do not merge these operations because all three involve signing in:

| Current path | Determining owner | Native presentation |
|---|---|---|
| Core command catalogue's Connect provider | `CatalogFieldSelection.connect_provider`, `widgets/comms_command_dialog.py:120–133`, calls Core `execution/login.py:run_login` | Textual currently suspends the terminal while the original installed Pi login UI runs. On exit zero, refresh catalogues with the original command arguments only if the selection is still attached; otherwise report the nonzero result and restore focus only on the live control |
| An ACP agent's advertised authentication method | `Conversation.provider_login`, `widgets/conversation.py:1386–1444`; `AgentSession.authenticate/reconnect_after_login` | Choose from that captured agent's advertised methods. Terminal methods run their original configured command/args/env/cwd in ActionModal; other methods use the original ACP authenticate request |
| Connector browser approval | Core `transport/connector.py:ApprovalSender` and `Connector.request_approval` | A QuestionKind message carries the bearer link to Core's durable human identity. The view displays the original message; it neither grants the request nor impersonates an agent to send it |

For the first path, `run_login` uses `ConfigOptions.configured().package`, strips thread launch state and runs the login control beside that package's original CLI. It supplies its own process environment and returns the actual process exit status. A Qt implementation must retain that installed-package selection rather than find another `pi` on PATH or copy its provider-authentication code.

For ACP terminal authentication, preserve the original sequence: run the chosen method; return on nonzero exit; wait until the original turn, submission queue and prompt-in-flight work permit restart; disable the prompt; call the session agent's `prepare_login`; then `reconnect_after_login`. The latter explicitly raises `LoginNotReady` unless the restarted session is ready. A zero terminal exit alone never displays login success. `CoordinationIdentity.prepare_login` preserves its existing history, unlike the other source's preparation callback; keep that family dispatch.

For nonterminal ACP authentication, `AgentSession.authenticate` retains `ClientSessionRequest` authority checks before the RPC, after it and after forced initialization. The UI cannot substitute a catalogue refresh for those checks. Picker cancellation restores focus without authenticating. Final cleanup re-enables only the remaining live native controls, under the phase-2 view/task lifetime.

The shared owner is `toad/client/provider_login.py`, containing the original selection/result/restart algorithm over the original session and command owners. Its native host contract is `choose_authentication(methods)`, `run_interactive(command)`, `show_login_failure(result)` and `restore_live_prompt_focus()`. Capture the original session/source before selection and recheck it after every await; use the phase-2 operation owner, not a provider registry or a detached fire-and-forget task. The picker returns its original advertised method or its explicit cancelled result.

Use the existing `TerminalExecution`, `Command.for_argv/for_script`, PtyProcess and TerminalOutcome family from `toad/terminal_execution.py` as the process owner. It already owns the PTY, incremental UTF-8 decoding, ANSI state, input writing, resize, one active acquisition and exact exit/signal result. Its ANSI parser/state in `toad/ansi/` uses Rich values, not Textual widgets; retain that one parser and do not add a Qt VT implementation. Separate the terminal-view contract used by attach/project (`set_state`, `set_write_to_stdin`, `is_attached`, `present_execution`) from the native paint implementation. Textual CommandPane remains its native Terminal view. Add `toad/qt/terminal.py: QtTerminalView(QAbstractScrollArea)` implementing that same contract: paint the existing ANSI character/style grid with QPainter, project alternate/scrollback changes, write actual key/input-method/paste bytes to TerminalExecution.write_stdin, and resize the original PTY in terminal columns/rows derived from this terminal's actual font metrics. This terminal-cell protocol is distinct from phase-5 history geometry; never convert a transcript row into a Qt history scroll distance.

`QtLoginDialog(QDialog)` owns that real terminal view and the original operation. Cancellation requests the existing operation's retirement and awaits close/reap before discarding the view; it does not assume that a cancelled waiter killed a child. Keep terminal output and the failed/unknown result visible, with native focus restored only if the captured prompt remains live. Completion does not imply ACP login readiness: the existing restart/handshake remains authoritative.

For catalogue provider login, extract Core execution/login.py's existing validated package/argv/environment construction to one `LoginInvocation` owner at that module. The existing run_login entry and the Qt PTY boundary both consume that invocation; neither looks for another pi nor copies its flags/provider environment. ThreadLaunch stripping, configured package, provider validation and socket/request removal remain at Core. This is a required owner extraction, not a function claimed to exist already. Preserve existing run_login callers, including the suspended Textual terminal path; the Qt boundary constructs Command.for_argv from the captured invocation and waits on TerminalExecution. Do not invent a login environment switch, replay an unknown authentication, or count an exit-code mock as native interactive proof.

The connector source was read on main `e44c874924f6383904f9c18ba2d91ea04faa249e`: `ApprovalSender` at `:461` owns service identity; `request_approval` at `:838–849` sends through that identity to the human owner. This is source evidence, not a native Qt approval run. The current approval notice does not require an agent called claude-ai. A test that declares that agent itself does not exercise its absence.

Existing Toad consumer: `tests/textual/test_catalog_command_actions.py`; session-change request controls include `tests/model/test_calls_pending_across_a_session_change.py`. Add shared controls for no advertised methods, cancelled picker, terminal exit failure, zero exit followed by a failed handshake, session replacement during RPC/login and late catalogue replies. Native checks must exercise actual interactive input, cancellation, focus and close while the login surface is open, using the configured provider only. Connector approval is checked separately against a bus without the borrowed agent name; it does not establish provider login or consume a model response.

## 6.7 Common startup and final native construction

Add `toad/client/startup.py` and `toad/client/cli.py`. Move the original DefaultCommandGroup.parse_args/format_usage, AcpCommand project-option precedence, get_agent_binding and the run/acp argument/AgentDefinition construction there. Introduce only the native entry contract `FrontendEntry`: create the native loop/application, run the captured launch, retain/report native failure and finish the original ApplicationOutcome. `make_cli(entry)` registers the common Click group/commands once. The native modules call that factory with their own entry declaration; they do not copy Click options or inspect a backend string.

Keep CoreConnection selection exactly once in parse_args, before default-command insertion. The typed Click context carries that captured connection and the native entry, rather than overwrite the captured value in a later callback. `InstalledAgentLaunch` owns the original project/agent selection; `AcpLaunch` owns the original custom AgentDefinition/session decoding. Their resolution invokes the original algorithms; no optional parameter bag guesses which launch kind it contains. Preserve -d/--project-dir precedence and its Click parameter-source record, NoAcpSessionId and invalid-agent diagnostics.

The native entry establishes its framework before asynchronous application work:

| Entry | Construction and loop owner | Failure/outcome owner |
|---|---|---|
| `toad.cli:main` / TextualFrontend | Use the ordinary asyncio Runner and original native ToadApp; await the actual App.run_async rather than nest app.run inside an already running loop | Move run_terminal's original escaping/retained-exception and return-code handling into its native async runner; keep persist_terminal_failure and call the returned outcome's finish after cleanup |
| `toad.qt.cli:main` / QtFrontend | Construct one QApplication before Runner; create corrected qasync.QEventLoop for it; resolve launch and construct one QtToadApp on that loop; await its owned close/outcome future | Qt close enters the phase-2 retirement coroutine before releasing QMainWindow/QApplication; retain the original ApplicationOutcome, including succession/reconnect, and finish it only after Runner cleanup |

Preserve launch_root.started and the multiprocessing `__main__.__spec__` setup at the entry boundary. Each native entry has a real package __main__ (existing toad/__main__.py; add toad/qt/__main__.py). Derive the spawn-skip spec from that entry's actual module/package; do not make a spawned helper import and construct a Qt or Textual application. --help/--version/settings must not construct QApplication, open a Core observation or authenticate. Native Textual replay and web-server commands are added by its entry extension; Qt does not claim to implement Textual web serving.

Extract `ClientApplication` to `toad/client/application.py`, composing the now-shared WorkspaceHost/WorkspaceSessionShutdown, transfer/command host and PreferenceHost contracts. Move the **original** logical initialization and application methods here: agent binding/project/session identity, ApplicationLifetime, navigation readers/navigation/actions, SidebarState, tab order/tracker, connection access, event binding and ApplicationOutcome handling. Preserve the original constructor's `TabOrder(home=WELCOME, db=DB(), scopes=connection.tab_scopes())` and original shared algorithms, not a native roster copy. Native factories initialize preparation/rendering, clipboard, attention, theme, workspace widgets and focus. ClientApplication supplies `initialize_client(agent_binding, project_dir, connection, agent_session_id)` but no native-widget constructor.

Selected native composition:

- Textual ToadApp keeps DeclaredWidgetActions and HostsNeovim, and composes ClientApplication with TextualWorkspaceHost/TextualTransfers and its original ThemedApp through the native host. Retain actions=(ApplicationAction,) and inherit_bindings=False on the actual Textual class. TextualWorkspaceHost supplies the real native CoreEventReceiver and ThemedApp once; shared WorkspaceHost no longer imports either. TextualTransfers retains native dialog/push_screen primitives, not a second application state.
- QtToadApp composes ClientApplication, QtWorkspaceHost, QtTransfers, the native preference/prompt/action capabilities, QtCoreEventReceiver and QMainWindow, with PyQtWidgetMeta. Its actual native children implement the same session/source/command contracts, not Textual's DOMNode methods. Do not inherit ToadApp, ThemedApp, HostsNeovim(App) or a hidden Textual application inside the Qt window. The Qt Neovim primitive owns the existing editor protocol/session through native input/view hooks, never a second editing-state mirror.

Constructor ordering is deliberately native, not an invented universal __init__: Textual retains prepare_spawn and native preparation before the original pre-super client setup, then initializes its actual framework once, then sidebar/theme-dependent placement, project reactive state and event binding. Qt initializes its actual QMainWindow/receiver on the running qasync loop before creating QObject-owned work; initialize native preparation and call initialize_client once; install input/lifetime hooks, construct workspace/sidebars/forms, attach settings effects and only then open observations/show the window. The initialize_client body is one shared provider. No constructor starts a second native App or loop.

Preserve the original reverse cleanup registration in ToadApp._close_all (:280 onward) through the phase-2 close protocol: stop accepting observations; retire navigation/actions and workspace scopes; finish accepted native preparation/acquired reads while the observer remains answerable; join render/navigation resources; close coordination access; then native attention/colours/background work. Keep AsyncResourceRelease's finally behavior so one failed release cannot strand later resources. Renderer-specific resources remain in that native app's release stack; no Qt shutdown calls Textual render-process methods merely to satisfy a nominal type.

## 6.8 Ordered work and verification

1. Extract the generic SettingsState and shared preference-host/effect bodies while retaining the concrete schema/reader at its real boundary. Migrate consumers to the actual generic or concrete role; keep stored identities and validation. Do not move native choice/editing bodies just to satisfy a module-name test (paper1 Theorem 4.8; paper4 Theorem 2.6).
2. Insert SettingsState, PreferenceHost, ClientApplication, NeovimBufferSession and native entry/receiver skeletons at existing source targets with insert_before_target/patch_target, then reindex. Promote unchanged original members only once those ancestors exist, and use binding-removing relocation for their completed modules. Pure setting choices move individually; the concrete SavedSettings schema/entry point stays native. Extract the Core LoginInvocation at execution/login.run_login and patch its real callers. The completed form scan supplies no automatic recipe for these moves; descriptor/default-schema, native startup/constructor, editor-receiver and PTY adaptations are the bounded authored stages, with the controls below. A missing extract-new-base operation never parks this batch.
3. Move the original store/event/effect/client algorithms once and delete those bodies from the native classes. The concrete store and its writer-owned entry point remain at their actual declaration; no generic-base entry point or old-path re-export is added. Migrate CLI/host/effect consumers to the selected shared or native owner and compose each native application through its shared parents.
4. Preserve original model/settings tests, and add only new native boundary cases for input completion, effects, file-write failure and application close. Do not port tests whose only assertion keeps a deleted alias alive.
5. Run native-free shared import discovery in a fresh interpreter, all changed-name consumers and actual native constructor/MRO checks. Preserve the real saved document on read/write and verify installed store discovery on a scratch copy before any live carry.
6. Run both native settings/command/history/tab flows with a real Core connection. Qt must close in the phase-2 order and preserve pending edits/write failures, stopped interests, source retirement and child destruction. This planning task does not run or claim that application.

Required existing tests include `test_saved_settings_write_back_unchanged`, `test_the_file_holds_what_the_user_has_set_even_a_value_equal_to_its_default`, `test_a_group_the_file_holds_empty_is_written_back_empty`, numeric-bound/non-JSON-number refusal, and all settings-screen tests. In particular keep `test_closing_the_form_sends_the_number_still_being_typed` and `test_a_newly_declared_setting_is_edited_applied_saved_and_read_back`. Keep `test_app_close_order.py` and its actual acquired-history retirement controls.

Add native controls for a refused focused edit during Save, a valid focused edit during Close, atomic write failure, edits arriving while a write runs, discard after explicit-default edits, a hidden/parked prompt following its setting, unknown saved theme, view destruction during an effect and restart reading exactly the stored keys. Use the real store/form owners rather than writing private state.
