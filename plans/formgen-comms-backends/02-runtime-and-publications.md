# 2. Runtime, publications and resource lifetime

**Planning status:** the shared/native split, persistent qasync loop and required loop-owner fixes are selected below. Native Python-3.14 validation is an implementation check, not a claim made here. The full handler/caller/import graph, native queue and exact class construction are still unfinished, so this phase is not executable yet.

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

**Open before ready:** specify the exact Qt event/pump class and barrier interception, and finish the original handler and carrier caller list. This is a named native ordering obligation, not a choice to drop the rule.

## 2.3 Operation kinds and native work

Create `src/formbridge/operations.py` for the generic kind/owner/resource contract. Preserve the original kind identity and exclusivity; derive the native Textual group spelling only at its native boundary. A shared operation kind never calls `DOMNode.run_worker`.

The generic operation owner controls start, replacement/cancellation and retirement. Concrete native resources expose their actual status/completion; do not mirror Worker/Task state into another writable enum or boolean collection. A running thread job remains owned after a future's cancellation request.

- Textual uses its original Worker and worker manager through the native operation member.
- Qt uses tasks on the persistent Qt asyncio loop through its native operation member.
- Core/helper and CPU work retain the existing bounded process/executor owners; do not put synchronous preparation on the UI loop.
- `ThreadInterestUpdate` remains an exclusive domain kind. Losing observation interest cancels the observation request according to the original connection family, not every acquired read indiscriminately.

**Open before ready:** list each original WorkerGroup subclass/caller and the exact generic/native operation classes and constructor hooks. The available generic API must not be invented from a `run_worker` spelling match.

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

## 2.6 Migration and checks

Required original consumers: every import/handler of CoreEvent, Subscription/CoreEventStream, CoreEventReceiver/CoreEventMessage and callback subscription variants; every WorkerGroup kind/start/cancel/status/decorator site; CommsHost, connection/subscriber owners, app/screen/widget inheritance and the publication-handler guard tests. The final caller list must also include native payload producers and stored/entry-point readers. It is not complete from the four source files above.

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

**Plan ready:** the exact shared/native classes, intended MRO/constructors, caller changes and NRA stages are recorded; a specific Qt-loop owner and queue/input design are selected; required owner fixes and native test recipes have exact targets and outcomes. The open design work above still prevents this claim.

**Implementation done:** those owner fixes and moves are applied, and the original Textual run plus a separate Qt run demonstrate the required delivery/read/cancellation ordering on the declared Python stack. An import-only pass or queued-callback test cannot certify native close. Those runs remain mandatory implementation evidence; this documentation-only task must specify them, not pretend the backend already exists.
