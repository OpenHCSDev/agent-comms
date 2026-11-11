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
| Access | `LocalCoreAccess(service)` | Original remote connection | Same Core ToolRequest; no UI-side command implementation |
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

**Still to finish:** exact public method signatures and native implementations for these effects, including every other use of CommsHost. Do not keep a vague `object` host and call this separation done.

### Preserve close ordering

Keep `CoordinationAccess.stop_observing` and `close` together. Stop the route timer/task, close thread interests, stop updates, retain the answering member, release subscriptions, join retirement work, then close the connection after acquired observations/reads are retired. A UI close must not cancel a write already accepted at its sink or block the UI loop while joining a helper.

The current `ROUTE_CHECK_SECONDS` declaration remains the one interval value. Qt must not copy a second number into a QTimer.

## 3.2 Share target context and command discovery

Destination `toad/client/commands.py`:

- Move `TargetContext`, `CommandContextChanged`, `ContextualCommand`, `ThreadCommand`, `ViewCommand`, `DirectMessagesCommand`, `CopyCommand`, `SortMembersCommand`, `CloseViewCommand`, `TargetSuggestion` and the generic command-catalog behavior together.
- Keep `TargetAction`, `TargetEdit`, `PreparedTargetAction`, field/editor declarations and ToolRequests at Core's current owners.
- Preserve `TargetContext.selection`, multi-target order, channel selection, actor/project and original observation. Native selections produce the original CommandTarget values rather than a string-keyed second target model.
- Preserve Core's action query and execution-time recheck. `ThreadCommand.available` does not grow another permission rule because the Core query already chose its membership.
- Retain `CommandCatalog`'s preparation-worker execution, command spelling precedence and recognized-command behavior when the target changes. A stale recognized command must not fall through as an agent prompt.

Remove `TargetContext.show_menu`'s Textual Region, screen, sidebar and worker code from this module. A native menu operation captures the original view/source before querying shared choices, checks that same view/source when the reply arrives, and invokes the original command. Clipboard, native menu position and opening a sort dialog are native effects; command meaning/availability and the selected target stay shared.

Move the conversation command contract below its native consumers so shared commands/catalogs can annotate it without TYPE_CHECKING or local imports. Its concrete operations are `command_target_context`, `read_command_catalog`, slash-command refresh and the shared host's feedback methods. A native Conversation inherits this contract; a type alias to a native widget is not the repair.

## 3.3 Extract the real command draft

Destination `src/toad/client/command_draft.py`:

- Move `PreparedChoices(AnnotationChoices, DeclaredMetadata)` unchanged as the catalog projection. Its `.parameter` is the original prepared field; its option list is not another manually maintained source.
- Move the logical `CommandText(Multiline)` hint here. The Textual five-row editor and Qt editor remain native declarations over that hint.
- Create `CommandDraft` from the existing `CommandDialog`'s actual draft state/algorithms: prepared action/core/target, original fields, choice projections, generated ObjectState, derived arguments, catalog refresh and confirmation preparation.
- The generated dataclass still comes from Core's target-field declarations and `compile_decoder(...).editor_annotation`; do not hand-write fields for each command or keep a second arguments store.
- `CommandDialog` and the Qt command dialog each hold this same draft and compose the shared form over its state. The draft no longer asks a native widget dictionary which fields exist.

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

- Keep `ThreadAction` as the original immutable action/request/context and `ThreadActionExecution` as the actual task plus captured source.
- Turn the current app-only `ThreadActions` holder into a shared application capability rather than an extra manager forwarding to `app`. It owns the pending execution map once. Migrate `app.thread_actions.invoke/pending/close` to the capability's explicit action-start/pending/close methods; list exact names after checking app method collisions.
- Start uses the source capture, rejects conflicting active resources and publishes the original ThreadRowStateChanged.
- Execute uses `coordination_access.write(captured, original_core.call, CommsTargetEditTool(...))` on the preparation worker. Core checks the write at its transaction boundary.
- Completion preserves retire-missing, reconnect-target and open-target behavior from the original CliResult; native feedback draws that original result.
- Finish clears only entries still owned by that exact execution and publishes the original change event. Shutdown joins accepted executions; it does not report them absent merely because a view task was cancelled.

Move `ThreadAction.collect`'s preparation/acceptance algorithm into this shared owner. Its modal-open effect is supplied by the native app. Remove the local import of CommandDialog and its direct screen APIs from shared code; both native dialogs return the same original argument outcome.

## 3.5 Callers, removals and checks

Required caller groups: app setup/close, every connection capture/write/read, subscriber widgets, target menus, sidebar/thread/conversation actions, slash command discovery/execute, command dialogs, model/provider selector and both form consumers. Search their imports and declarations across Toad, Core tools/tests and sibling packages before moving symbols; stored entry names are separate readers to check.

Delete the former moved declarations and native imports from shared modules in the same change. Keep native view members at native paths only when they still own behavior, not as re-exports. Use NRA's binding-removing relocation and source-checked member promotion where their stated preconditions hold. Complete the actual selector/member/import steps after the full scan; do not supply an invented cross-repository recipe.

Existing tests to retain/run include:

- `tests/textual/test_remote_core.py`: sidebar through connector, remote thread send, target-menu/root spelling, registry-slice reads, channel declaration reads and off-loop remote transcript preparation.
- `tests/textual/test_target_command_context.py`, `test_command_catalog_off_the_loop.py`, `test_command_pane.py`.
- `tests/guards/test_thread_action_dispatch.py`: declaration-owned action names, each mixed-target outcome once, Core refusal shown and the action released.
- `tests/guards/test_core_event_handlers_receive_their_event.py` and phase 2 resource tests.
- Field/draft tests: empty literal values, stale catalog/confirmation replies, focused pending edits, changed source/view, confirm-then-edit, provider login completion/failure and close during preparation.

**Ready/done:** exact host/native method signatures, execution-capability names, every caller/import change and Qt login presentation are supplied; the original shared bodies replace both native business implementations; stale-source, pending-action and Core permission tests pass; a real local and connector command flow works in both views. The current file is not that final execution recipe yet.
