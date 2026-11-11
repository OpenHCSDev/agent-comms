# 3. Connections, commands and drafts

**Planning status:** the original connection families, Core request boundary, command draft owner and source checks below are decided. The native host method list, full import/caller changes and Qt provider-login presentation still need finishing. Do not call this whole phase executable yet.

## Outcome

Both views use the same original connection, target context, command discovery, draft and execution algorithms. Core still decides which action is allowed and performs the write. Native code draws the menu/form, collects input, reports feedback and manages its native lifetime.

A local source capture and a remote source capture stay different types. A stopped observation can continue answering acquired reads. A closed/replaced view cannot apply a late menu, catalog or confirmation reply.

## 3.1 Move the original connection family together

Destination: `src/toad/client/connection.py`. Preserve these original declarations from `toad/comms_root.py`, with their current behavior:

- `CapturedSource`; `SelectedRoute(CapturedSource)`; `CurrentObservation(CapturedSource)`.
- `CoreConnection`; `LocalCoreConnection(LaunchRoot, CoreConnection)`; `RemoteCoreConnection(ProfileArgument, ProcessRemoteCoreAccess, CoreConnection)` and its opening errors.
- `Observation`; `NotObserving`; `FailedObservation`; `Observing(Observation, ReferencedCore, RegistryReferenceSource, RelationshipReads, ContextReads)`.
- `AnsweringReads(Observing)`; `LocalObserving`; `RemoteObserving`.
- `LocalAnsweringReads(AnsweringReads, LocalObserving, answers_for=LocalObserving)` and `RemoteAnsweringReads(AnsweringReads, RemoteObserving, answers_for=RemoteObserving)`.
- The complete registry-read family and `RegistryReads`, which retains one current answer and the original in-flight read rather than rereading the whole registry for each view.
- `CoordinationAccess(MroDispatch)` and its existing observation/interest/release behavior.

Keep the helpers' real owners too: transcript reads, references, relationship/context reads, notification status and async/sync resource release. Move a helper only if its import direction actually requires it; do not leave another implementation of it behind.

### Required differences

| Fact | Local | Remote | Readiness/close requirement |
|---|---|---|---|
| Source capture | `SelectedRoute.selection` | `CurrentObservation.observing` | Confirm the held capture before the actual write; never substitute a current root-name lookup |
| Root spelling | Resolve a local path | Retain Core's spelling | Do not normalize a remote path as a local filesystem path |
| User thread | Local service may claim/read the checkout's user | Served registry supplies the remote user | Do not claim a remote user on this machine |
| Access | `LocalCoreAccess(service, original_observation_process)` | Original remote connection | Same Core ToolRequest and original process owner; no UI-side command implementation or second read pool |
| Observation stop | Local answering member | Remote answering member | Updates/interests stop, acquired reads retain their process/release owners |

`AnsweringReads.__init_subclass__` already declares each stopped/active pair. Preserve that derivation and the empty crossed members. Do not replace it with `remote`, `stopped` or `accepts_updates` flags.

### Remove the native host dependency

Move `CommsHost` into `toad/client/host.py`, retaining its source/tab/session/project/preparation requirements. Remove ThemedApp and DOMNode from this shared contract; native apps compose their own native bases.

Move `SidebarSubscriber` and `NotificationSubscriber` into `toad/client/subscribers.py` with ordinary ABC contracts. Their current `AbstractHooksMeta` import is Textual-specific. The native Textual classes retain the framework's metaclass where needed; the shared contract does not import it indirectly under a new name.

Make these native effects explicit on the host/runtime contract, then update each original caller once:

- `CoordinationAccess.start`: own a periodic call and its stop resource instead of requiring Textual's `set_interval` return type.
- Its `OpenThreadStatus` callback uses the shared scheduling primitive, not an untyped copy of native `call_next` behavior.
- `Observing.receive_failed`: report the original error through a public native failure method, not `app._handle_exception` from shared code.
- Source/thread interest uses the original operation kinds through phase 2's native work owner.
- `succeed_onto(LaunchRuntime)` remains an application outcome/native process transition, not a duplicated route-selection algorithm.

### Selected host signatures and native owners

These are the intended declarations, not APIs claimed to exist already. `toad/client/host.py: CommsHost[Source]` composes the shared `CoreEventReceiver`, pure `BusPickerHost` and Core's existing `ResultShower`; it does not inherit ThemedApp. Retain the original typed fields `preparation`, `events`, `project_dir`, `tab_order` and `session_navigation`. Declare `running_loop: asyncio.AbstractEventLoop` as the native loop answer and a read-only `settings: ToadSettings` property, rather than making connection code depend on the preference store's write/save surface.

| Public method | Textual native implementation | Qt native implementation / requirement |
|---|---|---|
| `schedule_ui(callback: Callable[[], None]) -> None` | Original `call_next(callback)`: deferred until after the current message, never an inline call | Owner-thread queued delivery through the original UiThreadDispatcher mechanism, after QApplication and its proxy exist; callback failure must reach the fatal-error owner. The posted-outcome gap below is fixed first |
| `repeat_ui(seconds: float, callback: Callable[[], None]) -> ResourceRelease` | Acquire the original set_interval timer and register its stop on the returned original ResourceRelease | One UI-thread QTimer, parented to the native application, with its timeout directly invoking the synchronous callback on that thread. Release stops/disconnects it before native destruction; no asyncSlot or second route task |
| `report_fatal(error: Exception) -> None` | Route to the original Textual exception owner; this is not a refusal toast | Record/report the original exception through the native application failure owner and end that application. Do not turn an observation-service failure into an empty successful result |
| `show_warning(text: str, title: str) -> None` | Native warning notice | Native warning notice over the same text/title; no Core state change |
| `succeed_onto(runtime: LaunchRuntime) -> Never` | Preserve the original acknowledged call-from-thread handoff to `exit(Succeeded(runtime))`, followed by CancelledError in the initiating worker | Acknowledge the equivalent UI-thread outcome handoff before cancelling the initiating worker. Merely posting exit and returning to the worker is weaker than the original contract |
| `open_bus(choice: BusChoice) -> None`; `ask_peer_for_profile(found: PeerHoldsBus) -> None` | Original bus-picker/outcome behavior with native picker/work resources | Same BusChoice/profile/Core operations; native picker/work/outcome effects only |

`ResultShower.show_done(text, title)` and `show_error(error: Exception | str, title)` remain the existing Core result contract. The original exception/refusal logging policy in ThemedApp must be shared, not copied into Qt; its remaining notice/presentation split belongs in phase 6. Do not import Textual's SeverityLevel into this host. The existing warning-severity call in `transcript_publication.py:1050–1051` must be migrated with that split, not dropped.

Use native `TextualCommsHost` and `QtCommsHost` capabilities for these primitives. Textual composes its actual ThemedApp with the shared CommsHost; Qt composes CommsHost with its native application lifetime. Phase 6 must finish their complete concrete base lists and constructor order; this table fixes the host API, not a claim that the final Qt metaclass ran.

### Exact connection-call migration

In `comms_root.py`, change `OpenThreadStatus(app.call_next)` at `:854` and both `SidebarModel(self.app.call_next)` constructors at `:914,:926` to the shared `schedule_ui` contract. Core's `OpenThreadStatus` and `SidebarModel` both already accept `Callable[[Callable[[], None]], None]`; do not add an event/row adapter or another model. Change `Observing.receive_failed:701–704` to `report_fatal`, and the read-position warning at `:960` to `show_warning`. Change `sidebar_request:1077` from `saved_settings.current.sidebar` to the host's read-only `settings.sidebar`. Preserve the existing public running_loop attachment path at `:415`.

`CoordinationAccess.start:1160–1163` acquires `repeat_ui(ROUTE_CHECK_SECONDS, self.check_route)` into one ResourceRelease owner, then still calls check_route immediately. `check_route` is **synchronous** and creates the one route_task only when no current task is running. `observe_route` remains the original async body. In stop_observing, consume the timer release before cancelling/joining that original route_task. Remove the old optional native timer field and its None/stop/reset branches; do not create a coroutine timer or a second route worker.

### Existing dispatcher gap, not a new callback service

The original `MainThreadDispatcher.post` is not an interchangeable call_next today. Formbridge `services/main_thread_dispatch.py` has two concrete differences: without a delivery target, an owner-thread post can call inline; `DispatchedCall.run` captures callback errors in the call, while fire-and-forget post supplies no result reader. Qt `_UiThreadCallProxy._execute` just calls run, so that path does not surface the stored failure.

Fix the posted-call outcome at that existing mechanism before using it for the Qt host: posted failures need an explicit native failure destination, whereas synchronous call keeps its original authoritative return/error. A not-yet-started call cancelled during dispatcher shutdown is not a new fatal callback failure. The Qt application constructs its actual dispatcher only after QApplication exists and closes its owned queued calls before QObject teardown; never accept the inline fallback as the model scheduler. Do not create a second Qt callback registry or a wrapper at each consumer. The precise mechanism edit is a bounded Formbridge prerequisite; no current NRA recipe or native execution proof is claimed for it.

Required scheduler controls: no callback before schedule_ui returns; original FIFO/input boundary behavior; callback exception reaches the failure owner; close before delivery prevents the callback and releases its captured model; periodic release cannot restart the route task; several ticks during a slow route read retain one task; the synchronous route starter remains separate from its async work.

### Preserve close ordering

Keep `CoordinationAccess.stop_observing` and `close` together. Stop the route timer/task, close thread interests, stop updates, retain the answering member, release subscriptions, join retirement work, then close the connection after acquired observations/reads are retired. A UI close must not cancel a write already accepted at its sink or block the UI loop while joining a helper.

The current `ROUTE_CHECK_SECONDS` declaration remains the one interval value. Qt must not copy a second number into a QTimer.

## 3.2 Share target context and command discovery

Destination `toad/client/commands.py`:

- Move `TargetContext`, `CommandContextChanged`, `ContextualCommand`, `ThreadCommand`, `ViewCommand`, `DirectMessagesCommand`, `CopyCommand`, `SortMembersCommand`, `CloseViewCommand` and `TargetSuggestion` here. Move the original `ThreadAction` declaration/behavior here too, below the original command context. Keeping those two declarations together removes the present `target_commands -> thread_actions -> TYPE_CHECKING TargetContext` cycle; do not replace it with another guarded import.
- Declare the shared `CommandHost(NavigationHost)` and the small `CommandOrigin` contract here. TargetContext.app is CommandHost; ThreadAction's collection/completion algorithm refers to that contract, never to the concrete ThreadActions implementation. Both declarations are in the same module, so runtime annotations can resolve without importing a higher-level execution implementation.
- `ThreadActions(CommandHost)` and `ThreadActionExecution(TargetActionState)` live in `toad/client/thread_actions.py` and import their original action/context from commands. CommandCatalog imports commands, not vice versa. Keep catalogue assembly/preparation in its existing owner under `toad/client/command_catalog.py`; the native menu owns the call that asks it for choices.
- Declare `TargetActionState` and `NoTargetAction` beside the action/context in commands; execution is the active member in the higher execution module. This is the actual absent/held-execution family, not a record copying its label.
- Keep `TargetAction`, `TargetEdit`, `PreparedTargetAction`, field/editor declarations and ToolRequests at Core's current owners.
- Preserve `TargetContext.selection`, multi-target order, channel selection, actor/project and original observation. Native selections produce the original CommandTarget values rather than a string-keyed second target model.
- Preserve Core's action query and execution-time recheck. `ThreadCommand.available` does not grow another permission rule because the Core query already chose its membership.
- Retain `CommandCatalog`'s preparation-worker execution, command spelling precedence and recognized-command behavior when the target changes. A stale recognized command must not fall through as an agent prompt.

Remove `TargetContext.show_menu`, `command_choices` and `execute_menu` from the low context owner: their existing menu/catalog invocation moves to the native menu owner using the shared CommandCatalog, not a local import back into a higher module. The original context's selection/current/available_actions/acquired bodies remain shared. Remove Textual Region, screen, sidebar and worker code from this module. A native menu operation captures the original view/source before querying shared choices, checks that same view/source when the reply arrives, and invokes the original command. Clipboard, native menu position and opening a sort dialog are native effects; command meaning/availability and the selected target stay shared.

Move the conversation command contract below its native consumers so shared commands/catalogs can annotate it without TYPE_CHECKING or local imports. Its concrete operations are `command_target_context`, `read_command_catalog`, slash-command refresh and the shared host's feedback methods. A native Conversation inherits this contract; a type alias to a native widget is not the repair.

## 3.3 Extract the real command draft

Destination `src/toad/client/command_draft.py`:

- Move `PreparedChoices(AnnotationChoices, DeclaredMetadata)` unchanged as the catalog projection. Its `.parameter` is the original prepared field; its option list is not another manually maintained source.
- Move the logical `CommandText(Multiline)` hint here. The Textual five-row editor and Qt editor remain native declarations over that hint.
- Create `CommandDraft` from the existing `CommandDialog`'s actual draft state/algorithms: prepared action/core/target, original fields, choice projections, generated ObjectState, derived arguments, catalog refresh and confirmation preparation.
- The generated dataclass still comes from Core's target-field declarations and `compile_decoder(...).editor_annotation`; do not hand-write fields for each command or keep a second arguments store.
- `CommandDialog` and the Qt command dialog inherit `CommandDraft` alongside their native modal/form capabilities. The actual dialog is the draft; do not add a second draft object and forwarding methods beside it. Both compose the shared form over that original ObjectState, and the draft no longer asks a native widget dictionary which fields exist. The concrete constructor order and native primitives still belong in the remaining class map.

Move the original algorithm rather than simplify away its guards:

1. Finish the focused form edit before collecting arguments.
2. Disable apply while catalog/confirmation work is pending.
3. Snapshot arguments before off-loop preparation.
4. Discard a reply if its draft/view/source is no longer current or its arguments changed.
5. Update the original prepared choice projections without overwriting edited values.
6. Clear confirmation after an edit; recompute whether the action and catalogs are valid.
7. On Apply, run the same edited/confirmed action checks again and dismiss only if the same current arguments remain.

The native draft-view contract draws confirmation, enables apply, repaints affected choice controls and reports whether its original lifetime is still active. It does not query Core or reconstruct command validity. Its native callback closes with the view, using phase 2's work/subscription resources.

### Provider login is not a no-op

[Phase 6's login section](06-preferences-and-native-application.md#66-login-retain-the-actual-request-and-readiness-owner) records the three actual login paths, original request/readiness owners and required native checks. The command catalogue's provider action is part of this complete command flow, not an optional button to omit while the interactive Qt terminal boundary remains unfinished.

## 3.4 Execution remains one shared operation

Destination `src/toad/client/thread_actions.py`:

- Keep the original immutable `ThreadAction` action/request/context and its collection/completion behavior at the lower commands owner specified above. `ThreadActionExecution` remains in thread_actions as the actual execution/captured-source state; do not leave a duplicate ThreadAction declaration here.
- Turn the current app-only `ThreadActions` holder into a shared application capability rather than another object forwarding to app. It owns the execution map once. Its selected public names are `start_target_action(action: ThreadAction, subject: str, *, source_root: Path) -> None`, `target_action(subject: str | Path) -> TargetActionState`, `target_action_finished(execution: ThreadActionExecution) -> None`, and `async finish_target_actions() -> None`. The current source search found no production declarations of those names; the complete native MRO remains a required constructor/name check.
- Start uses the source capture, rejects conflicting active resources and publishes the original ThreadRowStateChanged.
- Execute uses `coordination_access.write(captured, original_core.call, CommsTargetEditTool(...))` on the preparation worker. Core checks the write at its transaction boundary.
- Completion preserves retire-missing, reconnect-target and open-target behavior from the original CliResult; native feedback draws that original result.
- Finish clears only entries still owned by that exact execution and publishes the original change event. Shutdown joins accepted executions; it does not report them absent merely because a view task was cancelled.

### Pending state and startup order

Do not preserve the current pending-dictionary algorithm. `ThreadActions.pending` at `thread_actions.py:35–36` reconstructs all execution labels; `ThreadStatusRow.show` at `widgets/comms_sidebar.py:239` calls it once per rendered row. With r rows and a action-resource entries, this adds O(r*a) work for a set of point lookups. Rendering n rows with n entries can therefore become quadratic.

Make `TargetActionState` the public pending-state base. `NoTargetAction(TargetActionState)` supplies no-pending behavior; the existing `ThreadActionExecution(TargetActionState)` is the actual active state, not a second record alongside it. Declare `is_pending` on that family and `summary(row: ThreadRowModel) -> str` / `busy(row: ThreadRowModel) -> bool`: the no-action member uses the original row's summary/working state; the execution uses its original action.pending and active-action behavior. `target_action(subject)` is one map lookup returning the held execution or NoTargetAction. Do not reconstruct a label dictionary, cache another status string or expose the concrete task for a renderer to inspect.

`ThreadStatusRow` holds that family member instead of optional action_status, and calls its summary/busy methods. Both native rows use that same projection. Finish removes only map entries still owned by the exact execution. Membership checks in `tests/native/test_native_journey.py:282` and `tests/textual/test_archive_from_thread_menu.py:82` become queries of target_action(...).is_pending, not iteration over a copied map.

Reserve/register every action resource and publish its original starting change before native work can execute. `ThreadActionExecution.__init__` currently starts the worker itself; do not copy that side effect into a Qt create_task constructor where eager execution could finish before the map is populated. The phase-2 operation owner must reserve the resource before starting it and retain completion even if the work completes immediately. Test eager/immediate completion, failed native start, conflicting resources, a project-path resource, and an old completion arriving after a successor owns the same key.

Exact consumers: remove `self.thread_actions = ThreadActions(self)` at app.py:193; register `finish_target_actions` in the original shutdown stack at :307; change the accepted call in `ThreadAction.collect` at thread_actions.py:134 to start_target_action; change `ThreadStatusRow.show` to target_action; migrate the two test queries above and the imported execution in `tests/guards/test_thread_action_dispatch.py`. The refusal test must call the real capability/owner rather than preserve its SimpleNamespace(app, finished) fixture. Keep mixed-target outcome and real Core-refusal behavior. A deleted-name guard is not authority to retain the manager or forbid the new capability; remove assertions that only keep old names deleted instead of porting that roster.

Keep the original `ThreadAction.collect` collection/acceptance body with its declaration in commands, calling CommandHost's action-start/native-presentation contracts. ThreadActions supplies the action-start/finish implementation; the native app supplies the modal effect. Remove owner.app from ThreadActionExecution: its owner is the actual ThreadActions application capability, so execution reads owner.coordination_access/preparation directly and finishes through target_action_finished.

`CommandHost.start_target_action` and target_action use the signatures above; their action/state types are declared in the same low module. `CommandHost.command_origin() -> CommandOrigin` captures the native presentation owner **before** off-loop command preparation. `CommandOrigin.current() -> bool` answers whether that actual original view/screen remains selected and live, and `async CommandOrigin.edit(prepared: PreparedTargetAction, title: str) -> dict[str, object] | None` runs the actual native dialog, returning the original argument mapping or cancellation. Recheck both the captured origin and original TargetContext before applying a reply. The native origin owns only presentation lifetime, not Core source permission. It does not reconstruct Core's captured route or command validity.

The modal branch uses that captured origin; a no-fields action already accepted by the user retains its original Core write semantics rather than being cancelled just because a later tab became selected. The native CommandOrigin implementations must use the phase-4 actual view/source identity, not a mode-name string that can be reused after closing/reopening. Their concrete native classes/constructors remain in the phase-4/6 class work; the shared signature and dependency direction are fixed here. Remove the local import of CommandDialog and its direct screen APIs from shared code; both native dialogs return the same original argument outcome.

## 3.5 Callers, removals and checks

Required caller groups: app setup/close, every connection capture/write/read, subscriber widgets, target menus, sidebar/thread/conversation actions, slash command discovery/execute, command dialogs, model/provider selector and both form consumers. Search their imports and declarations across Toad, Core tools/tests and sibling packages before moving symbols; stored entry names are separate readers to check.

Delete the former moved declarations and native imports from shared modules in the same change. Keep native view members at native paths only when they still own behavior, not as re-exports. Use NRA's binding-removing relocation and source-checked member promotion where their stated preconditions hold. Complete the actual selector/member/import steps after the full scan; do not supply an invented cross-repository recipe.

Existing tests to retain/run include:

- `tests/textual/test_remote_core.py`: sidebar through connector, remote thread send, target-menu/root spelling, registry-slice reads, channel declaration reads and off-loop remote transcript preparation.
- `tests/textual/test_target_command_context.py`, `test_command_catalog_off_the_loop.py`, `test_command_pane.py`.
- `tests/guards/test_thread_action_dispatch.py`: declaration-owned action names, each mixed-target outcome once, Core refusal shown and the action released.
- `tests/guards/test_core_event_handlers_receive_their_event.py` and phase 2 resource tests.
- Field/draft tests: empty literal values, stale catalog/confirmation replies, focused pending edits, changed source/view, confirm-then-edit, provider login completion/failure and close during preparation.
