# Current source and owners

These revisions record a source read, not a package freeze. Implementation uses refreshed current mains. The owning rule is [no-waiting.md](../../src/agent_comms/team/rules/no-waiting.md), under “Our own packages track latest main”.

## Source acquisition

Core planning uses the owned Core worktree on a branch from freshly fetched main. Toad uses the existing owned Toad checkout, refreshed from `origin/main`. Qt, Formbridge and their model/introspection dependencies were refreshed through the declared main branches in an owned OpenHCS source tree. The Qt source is also acquired through Core's `comms_repository_read`; that tool supports unregistered repositories and grants no edits.

Do not reproduce the stale P142 comparison by reading an old pinned OpenHCS child checkout beside current installed packages. Refresh the source first. No interpreter/environment or application was rebuilt to perform these source reads.

## Read receipts, 2026-11-10

| Source | Revision read | Role |
|---|---|---|
| agent-comms | `7edd87580ffa41dd0e2a50f694f630c7145ff199` | Current main base of planning branch |
| Toad | `cdc6a761b476bd0b25bd323d88860cf5f7d56fe4` | Freshly fetched main |
| PyQtReactive | `b11a218ebffc992bf7ed1a3798bc6a609c48d7f3` | Refreshed main, including native Formbridge migration |
| Formbridge | `0547625fd956077b3130fb1bad9e942f38783149` | Refreshed main |
| ObjectState | `79c8b94b18a259ffb099fe1d18d21c1af525102a` | Refreshed main; replaces the older installed model source for planning |
| python-introspect | `f2876b9ee089c65789607284949e11a9f58283fe` | Refreshed main |
| metaclass-registry | `60138d533ef0758c5f093d9110ba9f5656f0934a` | Refreshed main |

A later commit in one of these packages requires checking the affected claims, not retaining this table as a compatibility target.

## Rechecked P142 findings

### Conversion calls: already fixed

Qt `src/pyqt_reactive/forms/parameter_form_manager.py:779–781,833–837` calls the current three-argument conversion API. Its `pyproject.toml` requires Formbridge 0.2, ObjectState 5 and python-introspect 0.3. Current Formbridge `src/formbridge/forms/parameter_form_service.py:286–288` declares the same API.

The older Qt checkout used for P142 predated those changes. No signature migration, old-call shim, version probe or dependency downgrade belongs in these phases.

### Nested paths: already fixed

Qt `src/pyqt_reactive/services/field_change_dispatcher.py:38–54` constructs one `FieldDispatchContext.field_path: DottedFieldPath` from the form's existing path owner. `SourceStateUpdateStage` at `:111–117` and `RootSignalStage` at `:299` both use `field_path.value`. The former ancestor-concatenation method is absent.

Keep that owner and preserve its behavior. There is no second path reconstruction to delete on current main.

## Source map for phase design

Paths in this table are relative to each package's `src/` directory. They identify existing owners, not a promise that the entire module can move unchanged.

| Fact/behavior | Existing declarations | Native dependencies to separate |
|---|---|---|
| Field identity, metadata, validation and write | Formbridge `formbridge/forms/declared_fields.py`: `DeclaredField`, `OptionalMemberField`, `FieldPath` | None in the declaration owner |
| Field/view membership | Formbridge `DeclaredViews`, `DeclaredMetadata`; parameter-info family and `parameters_for_state` | Each renderer's own widget base and native type translation |
| Values, paths, history and dirty changes | ObjectState and `DottedFieldPath` | UI delivery of its notifications, not a second state |
| Textual form behavior | Toad `toad/forms/editors.py`, `toad/forms/form_view.py` | Input events, Content, lazy mount, visibility and repaint |
| Qt form behavior | Qt `pyqt_reactive/forms/parameter_form_manager.py`, `widget_creation_config.py`, `widget_strategies.py`; field dispatcher | Qt signals/timers, native validation states, layouts and chrome |
| Queued owner-thread work | Formbridge `services/main_thread_dispatch.py`; Qt `services/ui_thread_dispatch.py` | Qt `_UiThreadCallProxy` and owner-thread detection |
| Coroutine jobs | Formbridge `services/async_operation_executor.py` | One loop per job; does not yet provide persistent application execution |
| Event/subscription resource | Toad `toad/core/events.py`, `toad/core_event_carrier.py` | DOMNode, Textual callback invocation, WorkerGroup and failure drawing |
| App/source host contracts | Toad `toad/comms_host.py`, `toad/comms_subscribers.py`, `toad/session_projection.py` | ThemedApp and AbstractHooksMeta |
| Connection/capture/observation | Toad `toad/comms_root.py`: original local/remote, failed, observing and answering-only families | Native scheduling, host methods and subscriber delivery |
| Conversation identity/destination | Toad `toad/conversation_kind.py`, `toad/navigation_target.py`; Core thread/channel values | Prompt/activity construction and native navigation |
| Submission transaction | Toad `toad/conversation_submission.py`, `toad/bus_submission.py` | View feedback, worker scheduling and scroll operations |
| Command meaning/execution | Core `agent_comms/cli_verbs/cli_command.py`, `target_fields.py`, served tools | No native UI authority |
| Command draft and UI transaction | Toad `toad/target_commands.py`, `toad/thread_actions.py`, `widgets/comms_command_dialog.py` | Region/menu/dialog APIs and native task resources |
| Logical tabs and navigation | Toad `toad/tab_identity/`, `tab_order.py`, `session_navigation.py`, `session_entry.py` | Screen factories, focus, native attach/detach and persistence access |
| Sidebar selection and presentation | Core `ui_model/sidebar.py`; Toad `sidebar_navigation.py`, `sidebar_model_view.py`, `widgets/comms_sidebar.py` | Real row lookup, mounting, geometry and paint |
| Message pages, cursors and original proofs | Core `messaging/message_page.py`, `page_traversal.py`, `read_basis.py`; Toad `wire_history.py` | Some current fragment/source classes still import native views |
| Actual painted read evidence | Toad `widgets/wire_history.py`, `widgets/message_notifications.py`; Core mark-page request and `history_views.py` | Original terminal strips/publication; Qt must supply actual native paint |
| Saved history coverage | Toad `history_coverage.py`, transcript source/preparation owners | Native document park/render lifetime and AbstractHooksMeta |
| Preferences | Toad `preferences.py`, `setting_declarations.py`, `setting_effects.py`, `screens/settings.py` | Native widget effects, terminal dimensions/title, key handling and theme application |

## Tests already located

These are existing requirements to read and retain, not tests reported as run during planning:

- Formbridge: `tests/test_declared_fields.py`, `test_declared_field_classes.py`, `test_declared_views.py`, `test_form_declaration_gates.py`, `test_no_toolkit_imports.py`, `test_main_thread_dispatch.py`, `test_async_operation_executor.py`.
- Qt: `tests/test_annotation_choice_fields.py`, `test_field_mutation_authorization.py`, `test_form_build_transaction.py`, `test_parameter_form_tree_index.py`, `test_widgets.py`, `test_ui_thread_dispatch.py`.
- Toad forms/commands: `tests/textual/test_forms.py`, `test_target_command_context.py`, `test_command_catalog_off_the_loop.py`, `test_command_pane.py`; `tests/guards/test_core_event_handlers_receive_their_event.py`.
- Toad tabs/history: `tests/model/test_tab_scopes.py`, `test_tab_visit_history.py`; `tests/app/test_tab_order.py`, `test_tab_history.py`, `test_channel_tab_sharing.py`; `tests/textual/test_stale_tab_restore.py`, `test_parked_tab_arrives_progressively.py`, `test_parked_session_tab_arrives_progressively.py`.

Exact test selectors, further consumers and any new missing cases belong in each phase document after its current source trace.

## Runtime investigation in progress

The current Qt package does not already integrate a persistent asyncio UI loop. Formbridge's `AsyncOperationExecutor._run_coroutine` creates and closes a loop per submitted job, so it cannot simply own a long-lived observation task.

The upstream qasync source was acquired read-only as a candidate: `d47e582` at inspection. It supports PyQt6, but its README still states Python `<3.14` while current Core requires 3.14; project metadata alone does not settle this difference. No qasync dependency was added, imported into an undeclared environment, or declared working. Its suitability needs source and native execution evidence before phase 2 can be ready.

The name “Formgen” is the shared-form/application goal here. The acquired package owner is Formbridge. A lookup for `OpenHCSDev/FormGen` returned repository-not-found; no separate repository or implementation is assumed from the product name.
