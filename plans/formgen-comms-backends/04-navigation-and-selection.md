# 4. Tabs, navigation and sidebar selection

**Planning status:** the logical owners and transition order below are decided. Exact native factory/base changes and the complete caller/import list still need finishing. This phase is not yet a finished execution recipe.

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

Split native scroll position from shared selection. The existing terminal `panel_scroll_y` is in terminal layout units; Qt must not interpret it as pixels. Keep renderer-specific position on native SidebarState members, while selected identities, expanded groups and the original applicable model remain shared.

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
