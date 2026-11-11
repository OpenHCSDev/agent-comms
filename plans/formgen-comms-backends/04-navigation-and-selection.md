# 4. Tabs, navigation and sidebar selection

**Planning status:** logical owners, transition order, native touch points and importer sites are traced on Toad main `1421d01d4` (§4.8), rechecked unchanged at `615575549`. The sidebar's actual scroll/frame/selection split is selected in §4.6. Base extraction uses the bounded existing insert/patch/reindex/member operations below; it does not wait for an absent dedicated recipe or claim a native execution proof.

## Outcome

Both views use the original tab identities, saved roster, visit history, session membership, source states and selected targets. Qt does not maintain another open-tab list or derive identity from the text of a tab/row.

Native code creates/mounts views, lays them out, focuses controls and reports when their presentation is ready. Logical registration is not proof that a view has mounted, become selected or painted its history.

## 4.1 Retain the actual owners

| Original owner | Shared destination | Required behavior |
|---|---|---|
| `tab_identity/` | `toad/client/tab_identity/` | Original typed identities and their mode names; thread birth and bus scope remain part of identity |
| `TabOrder` | `toad/client/tab_order.py` | Open order, scope-specific saved roster, visit history/cursor and return target |
| `SessionNavigation` and `Detached` | `toad/client/session_navigation.py` | Register/open/find, source checks, retired-view handling and dependent close |
| `SessionEntry` family | `toad/client/session_entry.py` | Logical identity, ownership/dependencies, source projection, tab presentation and return behavior |
| `SessionTracker`, `SessionDetails`, session-name and tab models | `toad/client/session_tracker.py` | One source of accepted names, thread/title projection and original open-session state |
| Logical `SessionScope`, origins, navigation/project/transcript contracts | Corresponding modules under `toad/client/` | Original source/project/thread/navigation answers without native widget bases |
| `WorkspaceSource` family and `WorkspaceSessions` | `toad/client/workspace_sessions.py` | Actual mounted views and selected source lifetime; no second native roster |
| `SidebarState`, `SidebarSelection`, selection/menu algorithms | Shared sidebar/navigation modules under `toad/client/` | Selection is about channel/target identities and model membership, not widget text |

These moves preserve Core's identities and the existing DB rows. Do not rename the durable format merely because Python modules move. Check `toad.db`'s real saved-tab readers and entry points; module-local re-exports are not a migration.

## 4.2 TabOrder moves without a new algorithm

Preserve the original `TabOrder` methods, including these easy-to-lose cases:

- `load` reads only the connection's TabScopes; other roots' stored rows stay untouched.
- `editing` serializes changes and joins the SQLite writer before releasing the lock, even if its caller is cancelled.
- `open(..., after=...)` changes open order only; `move` does not rewrite visit history.
- `navigate` moves the cursor when the step is taken, before a multi-frame native switch finishes. Two quick Back operations must count from successive positions.
- `record_visit` drops the forward branch before appending a new visit.
- `close` removes dependent identities from the visit list and repairs the cursor; return prefers the original eligible session rules, not the nearest native widget index.
- `project` reads current logical order over the original identity map. Native tab widgets follow that projection, not the other way around.

Its current Core retirement helper, DB and event types remain at their real owners. This is shared comms behavior, so it belongs in `toad.client`, not Formbridge with a new Core dependency.

## 4.3 Split the session contract from Textual

`SessionScope` currently imports ToadNode, AbstractHooksMeta, command Provider, DeclaredWidgetActions and SideBar, and implements Textual `_pre_process`. Its logical docstring does not make those imports generic.

- Shared `toad/client/session_scope.py: SessionScope` is an ABC for current/display/navigation/origin/thread/root answers and prepare/retire/close/activate/remove effects. `remove` returns an ordinary Awaitable, not Textual AwaitComplete.
- Shared `SessionOrigin`, `NoSessionOrigin`, `ProjectSessionOrigin` and `ViewSessionOrigin` retain their source/launch meaning. No-origin remains a distinct member that refuses; do not replace it with an optional widget probe.
- A native `TextualSessionScope` composes that contract with ToadNode. It retains native command-provider/key/focus/sidebar details and `_pre_process`'s failed-start detach behavior.
- A native `QtSessionScope` composes the same logical contract with the Qt view lifetime. Destruction detaches/retire-closes the original source before releasing the widget.
- `WorkspaceViews`, `NavigationHost`, `SessionNavigator` and `SessionCollection` lose Textual metaclasses/native return types. Declare the actual shared requirements and let native classes retain their own framework metaclass.

The exact concrete view bases and initialization winners must be checked for MainScreen, CommsScreen, preview/resident views and their Qt counterparts. A Protocol over the native class or a guarded import would leave the cycle in place.

## 4.4 Entries own meaning; native members create views

Keep the original entry family and split only its rendering effects:

- `NativeSessionEntry` already receives a factory. Generalize its MainScreen annotation to the shared session contract; preserve initial identity binding, original-thread fallback before mount, reconnect guards, source-status projection and return preference.
- `HistorySessionEntry` retains the CommsViewKey, viewer/project/participants, address, label/unread/reference projection and `ready` behavior. Its direct `CommsScreen(...)` construction moves to native entry members/factories.
- `PreviewSessionEntry`/`AcpLogSessionEntry` retain path/address/title and log classification. FilePreviewScreen/FilePreview/AcpLogPreview construction stays native.
- Native factories are selected by the app's declared entry family, not `if backend == ...` in the logical navigator. Creating another native view must not copy `history`, `register`, `detach` or `finish`.
- Logical `OpenTab`/`ThreadTab.label` must stop returning Textual Content. Retain the original title/row/unread data; native tab renderers draw it. Do not store a second label snapshot beside the original row.

Update `SessionNavigation.history_entry/history/native_entry/launch_entry/preview` and every other entry constructor to the selected native factory contract. Do not instantiate an abstract shared entry or retain a former native constructor as an alias.

## 4.5 Preserve the workspace switch in order

Move `WorkspaceSessions`' selection algorithm once and provide these native operations: ensure workspace, mount view, yield past due input, begin new frame/source presentation, choose chrome, apply default focus and remove native view.

The shared switch retains this sequence from the existing `select`:

1. Prepare the destination and reject failed/removed starts. If it is already shown, return it.
2. Retire/capture the departing source before hiding it; keep the parked resource.
3. Let due input/timers run, then revoke the departing frame before admitting destination preparation callbacks.
4. Set `LoadingWorkspaceSource(destination)`; hide the departing view and make the destination participate in layout.
5. Select native chrome, prepare the destination's presentation and let the native input/layout work complete.
6. Activate the session, apply native default focus and only then set `ShownWorkspaceSource`.

Preserve Detached/Loading/Shown/Parked as the original state family. The source setter retains the original VisiblePublication and derives its target from the held view/root. A native `visible` flag is not a substitute.

Closing retains the existing split: `detach` revokes logical membership/events and captures what is leaving; `finish` updates saved TabOrder, retires the selected presentation, chooses a valid return target and closes/removes each detached native view in its finally path. Dependent tabs close through `depends_on`, not by visual adjacency.

Native frame/input waits need actual native completion. `asyncio.sleep(0)` alone does not establish that Qt laid out or painted a view. Phase 5 supplies the paint/read part of this contract.

## 4.6 Sidebar selection and scroll

The shared owner retains current membership/selection logic from `SidebarNavigation`, including multi-target channel associations, a removed row falling back to its still-existing group header, current-session focus and route/revision guards during refresh.

Native rows are a projection of the same models. Qt item data stores the actual typed target/row; its foreign QModelIndex boundary is decoded once. A hidden/offscreen row still exists in the logical model even when no native widget has been allocated for it. Do not clear selection because a virtualized row lacks a QWidget.

Split native scroll position from shared selection. Shared `SidebarState` keeps `expanded`, `panels_collapsed`, `selected` and `selected_targets` at their original owner. `TextualSidebarState(SidebarState)` keeps the existing `panel_scroll_y: float = 0`; `QtSidebarState(SidebarState)` keeps its scrollbar's native position. Each native application constructs its corresponding state where `app.py` currently constructs SidebarState; consumers keep the shared base. There is one state object, not a second selection/scroll cache, and no row-to-pixel conversion.

The selected `SidebarNavigation` is the shared navigation capability on the actual native sidebar, not a new helper beside it. Its original `ready: asyncio.Event` and `revision` are initialized once before callbacks start. Remove the `sidebar` forwarding field and the native widget's construction of `SidebarNavigation(self)` together; its existing navigation caller obtains that same sidebar through the shared `NavigationRefresh` contract. `TextualSidebarNavigation(SidebarNavigation)` and `QtSidebarNavigation(SidebarNavigation)` supply native effects. Textual's concrete composition becomes `CommsSidebar(TextualSidebarNavigation, AutoHeight, SidebarModelView, SidebarVisibilityObserver, SearchFollower, SelectsTargets, GroupedSidebar, OpensSelected)`, preserving the original native base order after the new capability. In `CommsSidebar.__init__` (:378–388), retain native super construction, can_focus, then initialize the original navigation event/revision at the former helper-construction site before enabled/groups/mounting/open_views and before on_mount. The capability itself adds no competing native __init__. Select `QtCommsSidebar(QtSidebarNavigation, QTreeView, metaclass=PyQtWidgetMeta)` over a native QAbstractItemModel projection of the existing canonical SidebarModel, not another roster. It retains the original typed row/target in item data. Constructor order is native QTreeView, the original navigation event/revision, its one QtSidebarState/model reference, then model/selection-signal attachment and observation/publication subscriptions. No signal handler runs against a half-initialized navigation owner. Use the existing Qt metaclass selected in phase 1, not a new combined-meta copy. These are selected new declarations, not existing APIs or a claimed native MRO run.

| Original method/call | Shared algorithm retained | Native implementation |
|---|---|---|
| `state`, `restoring`, `reset`, constructor (:18–32) | Original state answer, event and revision lifetime | Native app supplies its one SidebarState; initialize the capability before native publication/subscriptions |
| `capture` (:34–36) | Only capture when navigation is not restoring | `capture_sidebar_scroll()` on Textual reads its actual `scroll_container.scroll_y` into TextualSidebarState; Qt reads its own scrollbar into QtSidebarState |
| `prepare` (:38–49) | Preserve revision capture, ready reset, route-change await and both revision/model checks | `hide_sidebar_rows()` performs the original `display = False`; Qt hides/revokes the corresponding stale native rows |
| `start`, `finish` (:51–68) | Original publication guard, revision comparison, model/display readiness, changed-scroll reschedule and final `ready.set()` | `present_sidebar_rows()` performs the original present; `after_sidebar_presented(callback)` is Textual `call_after_refresh` or Qt's actual completed-layout/paint boundary. It is not `sleep(0)` or a timer |
| `restore_scroll` (:98–105) | Called by the same shared finish loop; its result means the native scroll actually changed | Keep the complete original Textual body on `TextualSidebarNavigation`: model available, restoring, attached and identical current screen, then `scroll_to(y=state.panel_scroll_y, animate=False, immediate=True)` and compare before/after. Qt checks its actual attached/current view and restores only its native scrollbar position after layout, returning the actual change |
| `menu_context` (:71–83) | Preserve target order, deduplication, per-target channel association and original multi-selection replacement | The native row handler decodes its existing row to the original SidebarSelection once. `sidebar_target_context(selection)` returns the original TargetContext with captured view/root/mode/channel; the shared algorithm takes SidebarSelection rather than CommsRow |
| `apply` (:85–96) | Original selected-target reconciliation and group-header fallback | `has_sidebar_row(selection)` queries canonical model membership, not allocated QWidget existence; `apply_sidebar_selection()` and `sidebar_selection_change()` provide the original selection effect/context |
| `focus_current` (:107 onward) | Preserve current-session/first-row choice and ALL_COMMS_TARGET fallback | Keep native present, reveal-members and focus-row bodies on TextualSidebarNavigation; Qt applies that same canonical row order and selection to its actual native focus target. Do not copy source/route/revision reconciliation into it |

The shared host also exposes the original publication/model/display/current answers used by those algorithms, as declared properties over their existing owners. No method in shared SidebarNavigation names `scroll_container`, `call_after_refresh`, `screen`, `scroll_to` or a native row. The original Textual scroll and frame bodies stay native; their absence from an import list is not evidence that they disappeared. Native scroll restoration must complete before the common readiness event is set.

Migrate the original helper consumers together: `CommsSidebar.show_row_menu/on_mount/model_replaced/sync/_mount_groups/order_groups/selection_state/accepts_selection` at `widgets/comms_sidebar.py:397,407,427,459–460,479,498,526,532`; `screens/workspace.py:118,266,272`; `workspace_chrome.py:78,135`; and `session_navigation.py:111`. Replace their navigation-field forwarding with the sidebar's inherited methods/event/revision. The menu handler passes `selection_for(row)` rather than the native row. `GroupedSidebar` inherits the shared NavigationRefresh contract instead of declaring another `navigation` field; its existing change-application consumers call that contract directly. Check the full native MRO for method/field collisions before applying this batch, rather than preserving a helper alias to avoid the check.

Retain these refresh steps:

- A changed source hides/revokes stale rows before its first new frame. A same-source tab change may retain already valid rows.
- Capture/restore native scroll through its native owner, then wait for the resulting presentation before marking navigation ready.
- Menu choices use the captured original root/view. A delayed menu does not open on a replacement selection.
- The actual model/view decides whether a row exists; Core still decides target permission and membership facts.

## 4.7 Callers, removals, NRA and checks

Callers include app startup/select/close, WorkspaceScreen/SessionHost/chrome, all entry/identity constructors, thread navigation/open-history/back/return actions, channel/DM/context menus, sidebar models/rows, tab-title/unread presentations, saved-tab restore and DB writer-owned entry points. The final move uses the complete import/caller list, not only files whose names contain navigation.

Delete moved native factories/imports from the shared modules and migrate all original consumers in the same change. Retain actual native factory implementations, not old-path re-exports. Use binding-removing NRA relocation for whole pure declarations; method promotion requires the new parent contract and must retain constructor/lookup behavior. Record unsupported native base/factory checks rather than inventing a passing recipe.

Existing tests to preserve: `tests/model/test_tab_scopes.py`, `test_tab_visit_history.py`; `tests/app/test_tab_order.py`, `test_tab_history.py`, `test_channel_tab_sharing.py`; native restore/parking tests `test_stale_tab_restore.py`, `test_parked_tab_arrives_progressively.py`, `test_parked_session_tab_arrives_progressively.py`, plus `test_hidden_session_retirement.py` and command/remote navigation tests.

Add equivalent native Qt controls for rapid repeated Back/Forward, open-before-mount, shared channel tab, user-authored title, old thread birth replaced under the same name, source change during preparation, close during SQLite write, selected/dependent close, virtualized sidebar selection and scroll restore after layout. Keep original source/read objects in those tests; do not build a private-state fixture that bypasses navigation.

## 4.8 Execution map

Traced on Toad main `1421d01d4`, from every `ImportFrom` in `src/` and every native name inside the modules below. Recheck only the declarations a later main commit touches.

### What stops each module from being shared today

Every native name inside a module that §4.1 moves to `toad/client/`, with the method that uses it and where that use goes:

| Module | Native use (file:line, method) | Goes to |
|---|---|---|
| `tab_order.py` | none | moves as it is |
| `tab_identity/` (`__init__`, `kinds`, `history_origin`) | none directly; `__init__.py:13–14` imports `SessionEntry` and `SessionNavigation`, which are native today | moves last, after those two are clean |
| `session_tracker.py` | `textual.content.Content` returned by `OpenTab.label` (:39–40) and `ThreadTab.label` (:77–78) | shared `label` returns the existing title, unread and row data; the Textual tab renderer (`widgets/session_tabs.py`, the only native reader via its import at :39) builds `Content` from it |
| `session_scope.py` | `SessionOrigin(metaclass=AbstractHooksMeta)` (:27); `SessionScope.COMMANDS: ClassVar[set[type[Provider]]]` (:57); `SessionScope.sidebars() -> tuple[SideBar, ...]` (:100) | shared `SessionOrigin` and `SessionScope` become plain ABCs; `COMMANDS` and `sidebars` move to `TextualSessionScope`, whose ToadNode base already brings the Textual metaclass |
| `session_entry.py` | `MainScreen` as annotation: `SessionEntry.source` (:73), `NativeSessionEntry.__init__` factory (:102), `__call__` (:116), `source` (:126). Constructions: `CommsScreen(...)` in `HistorySessionEntry.__call__` (:228), `FilePreviewScreen(...)` in `PreviewSessionEntry.__call__` (:277), `FilePreview(...)` in `PreviewSessionEntry.content` (:281), `AcpLogPreview(...)` in `AcpLogSessionEntry.content` (:301) | annotations become the shared `SessionScope` contract; each construction becomes a member of that entry's native subclass (`TextualHistorySessionEntry`, `TextualPreviewSessionEntry`, `TextualAcpLogSessionEntry`), chosen by the app's declared entry family |
| `session_navigation.py` | `MainScreen` as annotation in `source` (:137), `bind_opened` (:196), `new` (:260), `native_entry` (:272); `MainScreen(...)` constructed in `launch_entry` (:357) and `start_agent_session` (:399); `reveal` (:100–110) queries `CommsSidebar`, `SideBar`, `SideBarCollapsible` | annotations become `SessionScope`; the two constructions go through the native entry factory; `reveal` keeps its session choice (`ordered_sessions[-1]`, the notify) and calls a native `reveal_sidebar()` for the widget queries |
| `workspace_sessions.py` | `sidebars()` on `WorkspaceSource` (:67), `DetachedWorkspaceSource` (:97), `BoundWorkspaceSource` (:141); `WorkspaceHost.workspace_screen: WorkspaceScreen` (:192); resident `WelcomeScreen` (:201); `query_one(SessionHost)` and `mount` in `prepare` (:226); `after_input()` in `select` (:239, :250) | the sidebar family moves to the Textual workspace source; the host annotation names the shared host contract; the native app supplies its resident views; `prepare` calls native `mount_view`; `select` calls native `yield_past_input` at both points, keeping the §4.5 order |
| `sidebar_navigation.py` | `capture` reads `scroll_container.scroll_y` (:34–36); `prepare` hides native rows (:49); `start`/`finish` present and call `call_after_refresh` (:51–68); `restore_scroll` tests native attachment/screen and calls `scroll_to` (:98–105); `focus_current` presents/reveals/focuses rows (:107 onward); `menu_context(row: CommsRow)` (:71) and `apply` read native rows | §4.6 assigns every original body/call to shared SidebarNavigation or TextualSidebarNavigation/QtSidebarNavigation. Native SidebarState subclasses retain their respective scroll units. The row handler supplies the existing SidebarSelection, and canonical model membership supplies selection existence |

### Order

1. `session_tracker`: move the `Content` building into `widgets/session_tabs.py`.
2. `session_scope`: move `COMMANDS` and `sidebars` down to `TextualSessionScope`; drop the metaclass from the shared ABCs.
3. `session_entry`: move each construction into its native entry subclass.
4. `session_navigation`: retype the annotations, route both constructions through the factory, split `reveal`.
5. `workspace_sessions`: the native mount, input-yield and resident operations.
6. `sidebar_navigation`: attach the shared navigation capability and its native effect capability to the actual sidebar, initialize its original event/revision before callbacks, remove the former helper construction/forwarder, split the scroll/frame/focus bodies, and migrate menu/apply callers as specified in §4.6. Construct the native SidebarState in the corresponding native app; retain shared selection fields and their original identities.
7. One relocation stage moves all eight now-pure modules (the `tab_identity` package last) to `toad/client/`.

Steps 1–6 must leave no Textual import in any module step 7 moves. The finish check below proves that.

### NRA operations

All available in the installed NRA:
- Step 7: `relocate_module_to_new_module` per module, in one stage. It rewrites every importer and removes the old binding; `retarget_module_mentions` follows string mentions. No re-export is kept.
- Steps 2, 3, 5 and 6: create the selected parents/native members with `insert_before_target`, attach their bases using `patch_target`, and rebuild the source index before any ancestor-only promotion. Move original native bodies with traced `insert_class_member`/`patch_target` stages and delete their former bodies in the same batch. Use `promote_class_members_to_ancestor` only after its actual ancestor and member-authority proofs apply. This includes TextualSessionScope, native entry/workspace members and both sidebar capabilities/state members. A dedicated extract-new-base operation is absent; the existing authoring operations are the selected route, not a reason to wait. Constructor/MRO/native-frame checks remain separate implementation obligations.
- Retyped annotations and parameters in steps 4 and 6: `replace_function_signature`.
- Moving a construction into a native member in steps 3 and 4: `replace_function_body` for the shared method, plus `insert_class_member` for the native one. The bodies moved are the original ones, not rewrites.

### Importers that the relocation rewrites

Production importers on main, per module. Test importers are rewritten by the same operation:

- `tab_identity` (12): `app.py:79`, `application_lifetime.py:17`, `core/events.py:30`, `db.py:25`, `navigation_preparation.py:20`, `session_entry.py:41`, `session_navigation.py:51`, `session_projection.py:12`, `session_tracker.py:13`, `tab_identity/history_origin.py:13`, `tab_identity/kinds.py:14`, `tab_order.py:11`; `kinds` also from `application_lifetime.py:16`, `session_navigation.py:52`, `thread_navigation.py:13`.
- `tab_order` (4): `app.py:80`, `comms_host.py:14`, `session_tracker.py:14`, `widgets/side_bar.py:69`.
- `session_navigation` (4): `app.py:71`, `core/session_requests.py:16`, `tab_identity/__init__.py:14`, `workspace_sessions.py:22`.
- `session_entry` (3): `session_navigation.py:42`, `tab_identity/__init__.py:13`, `tab_identity/kinds.py:13`.
- `session_tracker` (14): `app.py:73`, `conversation_kind.py:48`, `navigation_preparation.py:19`, `screens/main.py:56`, `screens/session_view.py:23`, `session_entry.py:33`, `session_host.py:18`, `session_navigation.py:50`, `session_projection.py:11`, `sidebar_navigation.py:11`, `thread_identity.py:21`, `widgets/comms_sidebar.py:41`, `widgets/session_tabs.py:39`, `widgets/side_bar.py:55`.
- `session_scope` (21): `app.py:72`, `command_target_scope.py:4`, `screens/comms.py:36`, `screens/main.py:55`, `screens/session_view.py:19`, `screens/welcome.py:32`, `session_entry.py:32`, `session_host.py:17`, `session_navigation.py:49`, `thread_navigation.py:23`, `transcript_publication.py:63`, `widgets/comms_chat.py:30`, `widgets/context_explorer.py:33`, `widgets/conversation.py:138`, `widgets/thread_comms.py:40`, `widgets/thread_identity.py:10`, `widgets/transcript_history.py:61`, `widgets/wire_history.py:17`, `wire_history.py:65`, `workspace_chrome.py:23`, `workspace_sessions.py:17`.
- `workspace_sessions` (1): `app.py:90`.
- `sidebar_navigation` (1): `widgets/comms_sidebar.py:71`.

The importers of `SessionScope` that read `COMMANDS` or `sidebars()` must import `TextualSessionScope` after step 2. A Textual screen that subclasses `SessionScope` today becomes a `TextualSessionScope`. The extract-new-base operation is exactly what rewrites those bases; until then it is part of the labelled stage.

### Finish check

- **No native imports in the shared namespace:** after step 7, an import of every module under `toad/client/` with Textual and Qt both blocked imports cleanly. That is the namespace test in [00](00-ownership-and-imports.md).
- **No old path left:** `git grep -nE "from toad\.(tab_order|session_navigation|session_entry|session_tracker|session_scope|workspace_sessions|sidebar_navigation|tab_identity)\b" -- src tests` returns nothing.
- **Behaviour:** the existing tests in §4.7 pass, plus the new Qt cases.
