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

The upstream qasync source was acquired read-only at `d47e582`. The plan now selects its QEventLoop through asyncio.Runner, with the owner fixes and native controls in phase 2. Its README still states Python `<3.14` while Core requires 3.14; project metadata alone proves no compatibility. The further source trace follows lost callback context/returned handles, qasync's executor field beside BaseEventLoop's executor field, and non-idempotent QThreadExecutor shutdown. No dependency was added, imported into an undeclared environment or declared working. Native Python-3.14 validation remains a required implementation check; P144 must specify that recipe, not implement the backend.

The name “Formgen” is the shared-form/application goal here. The acquired package owner is Formbridge. A lookup for `OpenHCSDev/FormGen` returned repository-not-found; no separate repository or implementation is assumed from the product name.

## Further source checks and NRA work

The current form/connection/command/navigation traces now have draft phase documents. The form check includes the real `ObjectStateFieldMutationService` bridge caller as well as widgets. Literal and inherited input remain separate because current readers give different answers for empty text, surrounding whitespace and the string `None`.

NRA source used: `5af050d8ed4855f8351751ef7236a31cf914705a`. The report paths cover Toad forms, Formbridge forms plus its ObjectState bridge, and Qt forms; context includes the full Toad, Core, Formbridge, Qt, ObjectState, python-introspect and metaclass-registry source roots. The Core source in this run is the planning branch at `70a3f785eb42a0e7d5d1301f7bde28b84cb6665c`; its Python files are kept unchanged while the run reads them.

The source-index-only command completed with exit 0: 1,537 context files, 43,694 source targets, including 147 class targets in the selected form/bridge files. It is useful for locating original declarations and their base spellings. It does not prove behavior, native class construction, complete findings coverage or a refactor recipe.

The full findings scans stopped at their 20-second and 240-second budgets. Those failed outputs are retained. The same full scan was run again with an 1,800-second budget and **finished with exit 0 in 196.48 seconds**: 6.147 seconds parsing, 189.276 analysis and 1.057 source indexing. Its source map covers 1,537 files and 43,694 targets; it reports three raw findings and no executable recipe (one safety rejection, two without a recipe evaluator). The shorter source-index command did not replace this run. Some cached work was reused; this is a completed run, not a measured speed improvement.

Full report SHA256: `295390c472acd21435ad36f9e9423ebc6feffb7d0bd7421e4654ea40731325db`.

| Finding | Source check and decision |
|---|---|
| `09e4d8f59b`: DeclaredField metadata filter supposedly mirrors Core FamilyMember | Rejected as an owner match. `DeclaredField.all_metadata` at `declared_fields.py:170–176` filters foreign annotation metadata with its caller-supplied type. It does not list Core Conversation/DeclaredFamily/Item members. Those unrelated names came from the tool's overlap search, not a required relationship. NRA also refused to synthesize a recipe. Keep this filter. |
| `4ed767f45b`: five-term form-build readiness condition | A lead, not a demonstrated copy. `FormBuildTransaction._schedule_finalization_if_complete` at `form_init_service.py:337–347` checks that its root is registered, registration/work have drained, no finalize is scheduled and the transaction is active. Preserve its short-circuit behavior and finalization tests; a long condition alone does not justify a new owner. No automatic recipe exists. |
| `2524e2137e`: raw `computed/project/initial_values` field metadata | The declarations are `ExtractedParameters` at `form_init_service.py:65–86`, consumed by `_extract_parameters` at `:511–531`. This is this package's own field-extraction format, not a duplicate ObjectState value model. The form phase must name a typed extraction-rule owner before changing it; no schema or safe rewrite was proved automatically. |

Read-only sampling during the 240-second run pointed through detector preparation/family collection and product-flow work. Sampling ranks where time went; no unprofiled speed improvement is claimed and no NRA implementation was changed by this assignment.

Scratch artifacts are under `/home/ts/.cache/agent-scratch/comms-toad-selector/`: `formgen-forms-nra-full.json` (20-second incomplete result), `formgen-forms-nra-complete.json` (240-second incomplete result), `formgen-forms-nra-source-index.json` (completed source targets), `formgen-nra-scan.svg` (profile), and `formgen-forms-nra-long.{json,log,exit}` (longer full run). These are investigation evidence, not package inputs or deployment pins.

## History and preferences follow-up

A further fetch of the owned Toad checkout returned exit 0 with both HEAD and `origin/main` still at `cdc6a761b476bd0b25bd323d88860cf5f7d56fe4`. Receipt: `p144-toad-refresh.{log,exit}` in the same scratch directory. This was a source refresh, not an app restart or new environment.

[Phase 5](05-history-and-read-evidence.md) now records the actual paint-to-proof path: Textual `ReadTranscriptPage.capture_render` and `RenderedTranscriptRows.admit`, `WireHistory`'s pending evidence/ReadAgain retirement, `ConversationKind.seen_page`, `WirePageBuffer.mark_page` and Core `MessagePage.select/message_evidence`. It also names source parking/retirement, original coverage owners, direct import consumers and real read-proof tests. Exact shared fragment bases, geometry units and Qt native admission remain unresolved; no source scan or native run is claimed for those moves.

[Phase 6](06-preferences-and-native-application.md) records `SavedSettings`'s original ObjectState, explicit-key/save/discard behavior, native effect dependencies, direct import consumers, durable preference entry point and current CLI construction. The root still imports native choice/effect/layout types. The plan therefore does not claim native-free settings, a Qt-only distribution or a resolved final application MRO.

The login follow-up distinguishes catalogue provider login (`CatalogFieldSelection.connect_provider` → Core `execution.login.run_login`), ACP method authentication (`Conversation.provider_login` → `AgentSession.authenticate/reconnect_after_login`, with `CoordinationIdentity.prepare_login` preserving native history), and connector browser approval. Core main `e44c874924f6383904f9c18ba2d91ea04faa249e` was read for `execution/login.py` and `transport/connector.py:461,838–849`; the latter sends as `ApprovalSender`, not a borrowed agent. These were source reads during another owner's install; no login, paid model call or second installer was run. The exact Qt interactive-terminal owner remains unresolved.

Both follow-ups are documentation. Existing tests are named as required future checks; none is reported as run here. The full NRA form-scan result above does not prove history or preference refactor recipes.

## Host, runtime and current-pair follow-up

The host trace fixes the zero-argument callback type from Core `ui_model/status.py:107` and `ui_model/sidebar.py:266`, and follows Toad `comms_root.py:854,914,926,1160–1205`. Route timer callbacks are synchronous; CoordinationAccess separately owns the actual async route_task. The selected host names and exact caller changes are in phase 3.

The next owned Toad fetch reached `e06e80266e773da13029f1fae3d7f76f6de92709`. The read-only repository tool timed out without a returned source result (`spawnSync agent-comms ETIMEDOUT`), so it was not retried. The already-fetched Git-ref comparison against the inspected cdc6a761 source shows only `comms_root.py` and a new channel-scrolling control changed. The production change is `LocalCoreAccess(self.service, self.process)` at `comms_root.py:792`; phase 3 now retains that original process argument. The inspected form, action/context, preference and wire-history owner files are unchanged by that source update. This is Git-object/source comparison, not an updated runtime or a native test.

I first inspected the stale `codex-textual-keys` tree for native callback behavior. After the current-main warning, I reread Textual main `19df8339e0421c4fda82d368f902e45d68c04df6` at `/home/ts/wt/claude-laptop--textual`: `message_pump.py:556–587,659–692`, `timer.py:93–101,190–214`, `app.py:3470–3510`. The selected call_next/timer/failure contracts still hold. This is a corrected source read, not a Toad/Textual application run.

Formbridge `services/main_thread_dispatch.py:39–95,160–170` and Qt `services/ui_thread_dispatch.py:17–47` show why post cannot simply replace call_next: its no-target owner-thread fallback can execute inline, and fire-and-forget callback errors are stored without a result reader. Phase 3 names the posted-outcome prerequisite at that existing owner, rather than copying callback handling into a new service.

The qasync follow-up reads `__init__.py:150–204,345–357,481–482,521–543,680–711` against installed Python-3.14 `asyncio/base_events.py:426,596–629,730–751,871–904`. Phase 2 now selects QEventLoop through Runner and specifies the lost context/handle, split executor-owner and repeated-shutdown repairs, with native controls still owed after implementation. No foreign loop patch or environment was created here.

The pending-action trace is Toad `thread_actions.py:35–36` plus `widgets/comms_sidebar.py:239`: rebuilding all pending labels once per row creates O(rows * resource entries) work. Phase 3 selects one lookup returning the existing execution/absent state, removing that reconstruction rather than caching its result.

The historical pair baseline is **reported**, not independently rerun by this planning task: claude-laptop's message `f980d35b9aee` records seven channel-scrolling failures in that run with Textual `19df8339e` and Core `b8da1aac0da36207c6d8d7b19972bf0e1707aee6`. Follow-ups `fe0611404bd6` and `f2cceeae55fb` identified the absolute-scroll assertion and flaky held-read control. The later correction `b089a2a8f95a` reports toad#1734 merged as `4a09b9d1db22df900472e1283d2faca216e89e28`: the held-read control could hold a retired request, hold a revision poll instead of the page, and check before the page reply arrived. The reported repaired control passed four branch runs, three rebased runs and its eight-test file; those are the author's results, not mine. That message also corrects growing-channel to intermittent, not consistently red. Its absolute scroll_y check still precedes painted rows/fragment/page origins, so neither that failure nor this source trace establishes reader drift. The control had already reached main through #1727/e06e80266; it was not contained in an open PR.

The common [completion contract](README.md#completion-contract) is owned by README; the phase documents retain their distinct consumer checks.

The Textual form follow-up reads original `forms/editors.py:38–313`, Formbridge `declared_fields.py:61–90`, current Textual `widget.py:3734–3778`, and Qt `widget_creation_types.py:65–90`. Phase 1 now records intended native Textual base lists, abstract/native metaclass composition, original constructor order and feedback/option/held-member primitives. It does not claim that those not-yet-implemented classes were instantiated. The bridge source at `bridge/object_state.py:498–510` also keeps reset_parameter distinct from update_parameter(converted_None), including the reset receipt; that distinction must survive the shared input-family move.
