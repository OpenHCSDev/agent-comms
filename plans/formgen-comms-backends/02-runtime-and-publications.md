# 2. Runtime, publications and resource lifetime

**Planning status:** the shared/native split, receiver-scoped Qt queue, accepted-input barriers, operation kinds and persistent qasync loop are selected below. Native Python-3.14 validation is an implementation check, not a claim made here. Apply this with the connection/session changes in phases 3–5; a half-moved receiver is not a usable checkpoint.

## Outcome

The original publication and subscription algorithms run once, shared by both backends. A native carrier schedules the original event, subscription and emission-time publisher; it does not reconstruct them from strings or query whatever source happens to be current later.

The application owns one persistent asyncio UI-thread loop. Native view resources attach/detach to that lifetime. A stopped view/source cannot accept a queued reply; an acquired read that the original connection permits to finish is still owned until it finishes. Cancellation does not turn an unknown send into a retry.

## Existing source, with the native dependencies exposed

- `toad/core/events.py`: `CoreEvent`, `CoalescingEvent`, `Subscription`, `ThreadChangeSubscription`, `CoreEventStream`. Subscription buckets are weak, listeners are weak bound methods, the stream borrows its publisher, and the receiver owns its subscription resources. `ThreadChangeSubscription` additionally owns thread-observation interest.
- `toad/core_event_carrier.py`: `CoreEventMessage`, callback subscription variants and `CoreEventReceiver`. The receiver currently combines `MroDispatch` and Textual `DOMNode`; `invoke`, bubbling, native message sender and pump-closing are native operations.
- `toad/worker_group.py`: kind-owned worker identity/exclusivity, but construction/cancellation/status currently call Textual directly.
- Formbridge `services/async_operation_executor.py`: a worker-pool operation owns a freshly created loop until that one coroutine finishes. `close()` cancels pending futures but does not stop a running coroutine. This is useful for its current job contract, not the application's persistent-loop owner.
- Qt `services/ui_thread_dispatch.py` implements the existing generic `MainThreadDispatcher` contract for synchronous owner-thread calls. It does not create a persistent asyncio application loop.

## 2.1 Generic publication mechanism: Formbridge

Create `src/formbridge/publications.py` with these public owners, moving the original algorithms rather than writing another event bus:

| Owner | Original source to move | Contract |
|---|---|---|
| `Publication` | Generic parts of `CoreEvent.subscribers/replaces/keyed_as` | Candidate subscribers follow the event's actual MRO; ordinary publications do not coalesce |
| `CoalescingPublication(Publication)` | `CoalescingEvent.coalescing_key/replaces/keyed_as` | A member defines its real key; no kind-string roster |
| `Subscription` | Original subscription resource except Core-specific annotations | Weak listener, exact active resource, identity-based acceptance, bucket-open/empty hooks, idempotent close |
| `PublicationStream` | Generic `CoreEventStream` subscribe/publish/publisher behavior | Snapshot matching resources before callbacks can close or subscribe; no scheduling or cached UI state |
| `PublicationDelivery` | Original event/subscription/publisher/consume ownership of `CoreEventMessage` | Original publisher is retained at emission; callback and ordinary deliveries retain their distinct consuming behavior |
| `PublicationReceiver` | Original subscription acquisition/observation/retirement and handler traversal | `MroDispatch` owns handler choice; native hooks enqueue, invoke and stop native propagation |

The generic stream must not learn thread names, bus roots, Core update types, native widgets or Textual Worker.

In `toad/client/events.py` retain the Core meanings:

- `CoreEvent(DeclaredFamily, Publication, affix="Event")` retains its nominal wire identity; no converter or renamed event string is introduced.
- `CoalescingEvent(CoreEvent, CoalescingPublication, declared_name=None)` retains the original coalescing capability. Check its actual MRO: the coalescing implementation must win over the ordinary Publication default.
- `CoreEventStream(PublicationStream)` retains thread-indexed buckets, `close_threads` and `observed_threads`.
- `ThreadChangeSubscription(Subscription)` retains its scope/bucket and interest-open/empty hooks; it schedules the original domain operation kind, not a native Worker directly.
- Preserve `ThreadCallbackSubscription`'s crossing of thread interest and callback delivery. Do not flatten it into two booleans or lose either close action.

Core event payloads remain the original values. Separate a truly native payload from shared event data before moving its class: the current Update/Thinking streams and AgentFail rendering code are explicit dependencies to resolve, not permission to annotate them `object` or import Textual into Formbridge. Native stream-only declarations may inherit the shared CoreEvent base without the shared module importing the renderer. No old-path re-export pretends a move is finished.

## 2.2 Receiver and native carriers

`toad/client/event_receiver.py: CoreEventReceiver(PublicationReceiver)` declares the Core event root and Core-specific resource operations without DOMNode or a native metaclass.

The Textual carrier in `toad/core_event_carrier.py` composes that receiver with DOMNode and its real message pump. Preserve these irreducible facts:

- ordinary publications bubble with the original sender;
- direct observations and callback deliveries do not bubble;
- `consume_publication` checks the original subscription at consumption, then `accepts`, before invoking handlers;
- a declaration expecting the event receives that event; the optional carrier argument is not substituted for it;
- handler invocation retains Textual's current callback/native-Worker behavior;
- `_message_pump_closing` retires subscriptions before continuing native close.

The Qt carrier in `toad/qt/event_carrier.py` carries the same `PublicationDelivery` through the Qt owner thread. Its native event/queued callback is a lifetime resource, not a second event record. A queued event is discarded after its subscription/view/source has retired, even if the Python wrapper for a deleted QObject is still alive.

### Coalescing and input order

Retain the original adjacent-only replacement rule: equal subscription/key can replace an adjacent queued publication, never an intervening input or nonreplaceable Comms update. Do not let a Qt-only side queue erase such a barrier because that queue never saw native input. The Qt carrier must either record accepted native input as a barrier in the ordered delivery owner or deliver without replacement until a faithful native barrier implementation is proved. It must not claim equivalent coalescing from a timer/debounce alone.

**Selected Qt scheduling:** add `QtDeliveryReceiver` and `QtDeliveryEvent(QEvent)` in `pyqt_reactive/services/publication_delivery.py`. The receiver is a native capability mixed into the actual QObject/widget, not a QObject manager beside it. It owns one deque and its actual drain Task; the QEvent is a registered native wake event, not another copy of the publication. `toad.qt.event_carrier.CoreEventReceiver` composes that native capability with `toad.client.event_receiver.CoreEventReceiver`. Keep the actual Qt control before its common QWidget/QObject branch and use the existing PyQtWidgetMeta.

One receiver has one FIFO drain coroutine. It awaits each original delivery's consume operation before taking the next; independent receivers retain independent tasks, matching Textual's separate message pumps. A single application-wide serial queue is specifically rejected: a handler awaiting another receiver's response could otherwise deadlock it. Drain exceptions reach the existing host failure path, and closing revokes the receiver's subscriptions and queued owner calls before cancelling/draining its task.

Publication enqueue uses the original shared delivery's replacement predicate, only against the adjacent tail, under the receiver's queue owner. Add nominal `NativeInputBarrier` and `OwnerCallDelivery` queue entries: neither can be replaced. The application installs one QObject event filter for accepted keyboard, mouse, wheel, touch, input-method and shortcut input. At that foreign Qt boundary, resolve the actual receiving widget's nearest native delivery receiver through QObject parenting and append its barrier **before** native dispatch can emit semantic signals. Native signals decode their values once and enqueue OwnerCallDelivery of the original shared operation on that same owner; they do not mutate shared state through a competing inline path. Ordinary Qt visual input remains native; the queued call is the semantic edit/command. Form controls use their bound manager/root receiver, and Save/finish is enqueued after earlier edits on that form, before document capture.

A posted publication from another thread first reaches the Qt owner through the corrected thread-safe dispatcher, retaining its emission-time publisher/context. The queue's acceptance order is that owner-thread admission order; no native input already admitted can be bypassed by later replacement. Install the input filter before subscriptions open. An input with no semantic callback still inserts a barrier. Do not enable tail replacement on a receiver whose native input bridge is absent: fail the construction check rather than silently use a Qt-only side queue. Tests must put actual native input between two replaceable deliveries and verify both deliveries survive in order; key/mouse-only mocks do not prove this interception.

Ordinary bubbling queues the original delivery on the live parent receiver after local consumption with the original sender/publisher retained. Observation/callback entries stop at their original receiver. Native close, source replacement and subscription.close make queued deliveries fail the existing acceptance check; a surviving Python wrapper is not authority to touch a deleted QObject. No nested processEvents loop or polling timer drives this queue.

## 2.3 Operation kinds and native work

Create `src/formbridge/operations.py` for the generic kind/owner/resource contract. Preserve the original kind identity and exclusivity; derive the native Textual group spelling only at its native boundary. A shared operation kind never calls `DOMNode.run_worker`.

The generic operation owner controls start, replacement/cancellation and retirement. Concrete native resources expose their actual status/completion; do not mirror Worker/Task state into another writable enum or boolean collection. A running thread job remains owned after a future's cancellation request.

- Textual uses its original Worker and worker manager through the native operation member.
- Qt uses tasks on the persistent Qt asyncio loop through its native operation member.
- Core/helper and CPU work retain the existing bounded process/executor owners; do not put synchronous preparation on the UI loop.
- `ThreadInterestUpdate` remains an exclusive domain kind. Losing observation interest cancels the observation request according to the original connection family, not every acquired read indiscriminately.

Use `OperationKind` and `OperationOwner` in Formbridge, with concrete native `TextualOperationOwner` and `QtOperationOwner`. The latter owns asyncio Tasks on the selected running loop; the former owns actual Textual Workers. Their common signatures are the existing kind operations: `run_on(owner, task, **options)`, `cancel_all(owner)` and `running_on(owner)`. `work` remains a Textual-only decorator; a shared method contains its original coroutine body, and the native entry starts that method through its kind. Remove decorators from moved shared methods, not their exclusivity or error behavior. A Qt task's done/cancelled/exception state and a Textual Worker's state are the authorities; no parallel execution-status enum is introduced.

Keep domain kinds beside the moved domain owner: ThreadInterestUpdate with client events, TeamDetailRead with the shared team read, RelationshipRowPresentation with the shared relationship read, CommsContentLoad with the corresponding connection/content operation, and InputHistoryRecord with shared input-history persistence if its caller moves. Each remains its original class identity/exclusive declaration, not a string group copy. Native-only kinds stay with their current renderer: StartedPublicationWork in transcript_publication, ProjectTreeMount in widgets/project_panel, PanelHydration in widgets/side_bar, CliDraftRender in widgets/cli_draft, and ContextReadWork/ContextNativeWork/ContextPresentationWork/ContextSearchWork/ContextDetailWork in widgets/context_explorer. Their Textual behavior is not imported into Formbridge merely because they share WorkerGroup.

Move the original kind/identity/exclusive algorithm from WorkerGroup to OperationKind; keep Textual group-name projection, worker decorator and Worker APIs on TextualOperationOwner at the native boundary. Migrate every kind import to its actual owner, including the separate InputHistoryRecord references, rather than leave worker_group as an aggregator. The original native worker kinds inherit the native owner capability; shared comms kinds are started by their host's native operation owner. Qt construction receives the already-running loop and receiver lifetime, never creates a loop per run. Source/view retirement owns cancellation and waits; acquired reads keep the original phase-3 release/answering-only behavior.

## 2.4 Qt loop: qasync, with fixes at its owner

Use `qasync.QEventLoop` on the Qt UI thread, for the application lifetime, through Python's `asyncio.Runner(loop_factory=...)`. The factory creates that loop for the already-created QApplication and installs it as the current loop. Do not use a second application thread, a per-operation loop or a replacement asyncio implementation. qasync is selected; its unmodified inspected source is **not** declared working on Python 3.14.

The first runtime implementation change is at qasync's owner, `src/qasync/__init__.py`, not a Toad wrapper around every callback. Preserve its public executor and loop behavior while fixing these traced contracts:

| Required fix | Complete source path | Change and required control |
|---|---|---|
| Thread-safe context and cancellation | `_QEventLoop.__init__:352–357` makes a signal of callback/args; `call_soon_threadsafe:680–682` emits only those values and returns None; the receiver calls `call_soon:541–543`, which creates a Handle later on the GUI thread | Create the appropriate cancellable handle at the calling thread, retaining explicit context or copying that caller's context, carry that same handle through the signal and return it. Native delivery schedules the existing handle, not a second callback/handle with GUI context. Preserve Python-3.14 thread-safe cancellation/run behavior; do not invent another locking implementation. Require distinct GUI/worker ContextVar values, an explicit third context, cancellation before delivery, closed-loop refusal and exactly-once execution |
| One default-executor owner | qasync writes `__default_executor` at `:345,702–711` and joins it in `close:481–482`. Python 3.14 BaseEventLoop initializes `_default_executor` at `base_events.py:426`; inherited `shutdown_default_executor:596–629` reads that other field | Use the inherited executor owner instead of two fields. Retain QThreadExecutor/default and explicitly supplied executor behavior, apply the inherited shutdown-admission check, and let Runner's async shutdown join off the UI thread while Qt can still answer authorized callbacks. Remove the second synchronous GUI-thread join, not just its symptom. Test default and explicit executors, to_thread, close without prior executor use and a worker awaiting an owner-thread callback during shutdown |
| Executor repeated shutdown | `QThreadExecutor.shutdown:191–204` currently raises after its first shutdown; BaseEventLoop's async shutdown joins and later `close:748–751` calls shutdown again with wait=False | Make the executor meet repeated-shutdown semantics: signal its workers once, allow later shutdown calls, and let a later wait=True still join. Test wait=False then wait=True, repeated close and actual worker retirement. Do not hide the second call with an app-specific catch |

This is a source trace: explicit context and the returned handle are lost in the inspected method; the qasync and inherited executor fields are distinct. It is not a native-run result or a promise of an automatic NRA recipe. Upstream's supported Python/binding combinations remain required checks for that owner change; do not silently drop them to fit Toad.

The native runtime then uses the corrected qasync declaration through its package dependency/lock. Record the source/version actually built, not a hand-installed patch in a shared interpreter. The package-selection recipe belongs in phase 6; a package that fails its Python-3.14 native controls does not ship the Qt backend.

Keep these additional integration requirements:

- Do not use `asyncClose:837–851`, which spins `QApplication.processEvents`, as application cleanup.
- Do not use `asyncSlot:855–891`'s module-global task set instead of the phase-2 view/source operation owner.
- `run_until_complete:414–440` performs a final native event pass. Revoke or drain deliveries before teardown so that pass cannot mutate closed views.
- Let the application coroutine finish its owned retirement while the Qt loop is still running; Runner then closes async generators/default executor/loop before QApplication is released.

Existing configured Qt Python is 3.12 with older Formbridge/ObjectState packages; inspected Python-3.14 environments have no Qt/qasync. No dependency, environment or native backend was created by this planning work. Native checks run on the declared implementation stack once built. Their absence is recorded evidence still owed, not a reason to invent a different architecture or demand that this documentation-only task implement the backend first.

## 2.5 Close sequence

One shared application lifetime owns this order; native code supplies close requests and final destruction:

1. Enter closing and reject new view/source operations; preserve already accepted/unknown transaction records.
2. Retire view subscription resources so already queued deliveries fail their acceptance check.
3. Publish/remove source-specific thread interests while their original observation owner still exists; then stop observations accepting new reads.
4. Cancel view-scoped native work. Retain acquired reads and already running worker/process resources according to their original contracts, and await their retirement without blocking the UI thread.
5. Detach ObjectState listeners, native signal connections, pending timers and paint/read publication resources before disposing their native objects.
6. Drain/join owned executor/process resources while the UI loop can still answer authorized callbacks. No new callback may acquire the closing view.
7. Finish the application coroutine, stop/close the native asyncio integration and then release the Qt application. Do not begin async cleanup for the first time from an already-exiting Qt loop.

A source replacement performs the corresponding source-scoped steps without closing unrelated views or the application. A parked view and a destroyed view keep their original distinction.

## 2.6 Declaration, caller and import closure

Keep the original member names while moving their determining bodies. The generic constructor/lookup map is:

| Destination | Constructor and implementation moved | Native remainder/deletion |
|---|---|---|
| PublicationReceiver | Original receiver initialization of subscription/observation resources; handler declaration traversal; subscribe/observe/retire/close algorithms | Native post/invoke/bubble/closing hooks only. Declare the publication/stream roots once on the Core specialization, not a repeated event roster |
| CallbackSubscription | Original weak-method acquisition in __post_init__ and callback retirement | consume calls the receiver's native invocation primitive with the original event; remove the direct Textual invoke import from the shared member |
| ThreadCallbackSubscription | ThreadChangeSubscription + CallbackSubscription, with original scope and dataclass initialization | Empty crossing class in client events; no callback/thread flags |
| PublicationDelivery | Original event/subscription/emission-time publisher and replacement decision | No Qt/Textual Message base, native sender or global current-source lookup |
| Textual CoreEventMessage | Keep `(event, subscription, consume=dispatch_publication)` at the native boundary, composing the shared delivery role with DeclaredMessage | Initialize the real Message once and the shared delivery fields once; native bubble/sender and pump replacement remain here. Do not construct a shared record then copy its fields into a second carrier record |
| Textual CoreEventReceiver | Client CoreEventReceiver + DOMNode and the original native metaclass | Retain @on(CoreEventMessage), Textual invoke, post_message and _message_pump_closing. Shared subscription algorithms are deleted here |
| Qt CoreEventReceiver | Client CoreEventReceiver + QtDeliveryReceiver on the actual native widget/owner | Real QEvent wake/queue, Qt parent/closing and invocation primitives; no DOMNode/Message imports |

Textual-only consumers stay on the native carrier, with the same event-as-first-handler-argument contract: widgets/conversation, comms_chat, prompt, side_bar, comms_sidebar, session_tabs, linked_rows, switch_toast, command_pane, thread_identity, sidebar_tree, model_switcher, observed_thread_activity, path_search, goal_bar; screens/comms, historical_sessions, session_view, source_modal, goal_edit, team and action_modal; plus native transcript_scope/transcript_source_preparation and goal_view members. Do not rewrite these imports to the pure receiver and lose their message pump. Shared logical users—comms_root, status_thread, agent_binding, conversation_submission/turn and the shared host/session contracts—import the generic delivery or client receiver. Their exact native/shared method splits are the phase-3/4/5 caller maps, not a reason to import the native carrier back into client.

Move the shared Core event declarations individually. AgentFail.present, native Update/Thinking stream payloads, LineBlock/Content summaries and other renderer-valued methods remain native, even when their enclosing event is logically about Core. A shared event contains the original identity/data and native view contracts; it never stores a Qt widget or Textual row under an object annotation to pass the import check. Phase 5 names the fragment/event members; native siblings inherit the shared identity, not a re-exported duplicate.

Ordered NRA batch: (1) insert/reindex the generic ancestors and Core specializations at existing source targets; (2) promote the whole original subscription/receiver methods where their ancestor proof applies; (3) relocate those completed declarations to their destination modules and migrate imports through the complete graph; (4) patch the explicit native carrier/constructor/invocation/queue boundaries; (5) promote guarded imports only through the existing full-graph operation, moving a declaration when it refuses. Remove the former bindings in that batch. Existing insert_before_target/patch_target author the new-base/native split today; a missing extract-new-base recipe does not stop it. The retained form scan did not preflight these runtime edits. Its bounded refusal is precisely the changed native invocation/constructor/scheduling behavior, which those structural operations do not prove; the required runtime controls below remain the behavioral proof.

Delete the old generic bodies/imports from core/events.py and core_event_carrier.py only after every moved consumer points to its new owner. Keep native files containing real native code; do not turn them into aggregation aliases. Update test imports for moved identities while keeping native pump tests on the native carrier. Search all original names across source, tests and tools, including decorators and string entry points, before applying the projected batch.

## 2.7 Migration and checks

Required original consumers: every import/handler of CoreEvent, Subscription/CoreEventStream, CoreEventReceiver/CoreEventMessage and callback subscription variants; every WorkerGroup kind/start/cancel/status/decorator site; CommsHost, connection/subscriber owners, app/screen/widget inheritance and the publication-handler guard tests. The current source search also includes native payload producers and stored/entry-point readers; use the split below rather than moving all carrier consumers into the shared namespace.

NRA structural operations available to investigate are `PromoteClassMembersToAncestorOperation` (selected direct members into an existing ancestor) and `RelocateSymbolsToNewModuleOperation` (exact symbols with former bindings removed). Their source preflights are not a proof of native metaclass/loop behavior. Do not use the binding-preserving move to leave aliases behind, or promote a method before its required parent/import direction exists.

Behavioral checks:

- original event identity, subtype handlers and event/carrier argument separation;
- weak listener disposal, double close, callback retirement during publish, subscription acquired/closed during the snapshot, original-publisher retention;
- ordinary bubbling versus direct observation/callback delivery;
- adjacent coalescing, different subscription/key, intervening input/Comms update;
- source/view replaced between enqueue and consume, source read finishing after observation stops, no late QObject mutation;
- exclusive replacement, cancellation, completion/error reporting, no lost worker resource;
- explicit/implicit context forwarding, GUI responsiveness during Core work, close during worker→UI dispatch and no nested event-pump wait;
- native close with queued callbacks, active reads, timers, worker failure and repeated close requests; no orphan task/helper/listener afterward.
