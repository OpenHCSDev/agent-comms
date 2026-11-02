# Patterns: the abstraction you are about to leave implicit, and its repair

Under-abstracted code has not avoided abstraction; it has failed to encode it. rules/slop-and-polymorphism.md owns that verdict and its proof (paper1 §2, Corollary 4.10): every pair the declared lattice does not derive, some consumer restates. No helper, forwarder, service or generator lowers that count (Theorem 4.6); multiple inheritance takes it to zero wherever needs cross (Proposition 4.2, Corollary 4.12); and an unnamed node is paid again per pair on every update, where a named one costs one declaration edit (Theorem 4.14, Corollary 6.2). So no pattern below is a mechanism swap, `isinstance` for `hasattr` or a method for an `if`. Each is a lattice node the requirement already contained (a shared provider, a contract, a nominal key, a family) named once, with the consumer copies deleted. This file is the reflex.

Paper1 §6.5 cites Graham's Blub paradox: unfamiliar capacity mistaken for complexity. The agent who says "I'm avoiding overengineering" wrote the same abstraction N times, unnamed, and called the copies simplicity. Blub talks in the vocabulary of good architecture: "clean separation", "a service per concern", "explicit is better than implicit", "duck typing is pythonic". The greentexts are that voice, in your words, with the real numbers at the end. Prove it isn't yours.

## Reflex check, before any new line

1. Which node does the requirement already contain here: a shared provider, a contract, a nominal key, a family, one owner of a value? Grep for its declaration. If it exists, use it and delete your copy.
2. If it doesn't exist, declare it once, on the class that owns it: a parent, a mixin, an ABC, a nominal type, a family base.
3. If you are still about to write the line, find it below: the left column is how an unnamed node shows up at a consumer.

| You are writing (the node it leaves unnamed) | Pattern | Repair (`--derive`) |
|---|---|---|
| if / isinstance / kind-string dispatch (the family) | IMPL-1, IMPL-3 | subclass, base-method |
| `hasattr` / `getattr` probes (the contract) | BOUND-7 | declaration (an ABC the classes inherit) |
| a helper, forwarder, manager or `*Service` (the shared provider or owner) | paper1 Thm 4.6 | mixin, or delete-copy into the owner |
| one procedure in several places (the shared provider) | IMPL-12, IMPL-5 | base-method |
| a parameter threaded through call sites (the owner that holds it) | IMPL-12 | base-method, declaration |
| a string standing in for identity (the nominal key) | MEMB-4, IDEN-3 | nominal-key |
| an optional `None` value, branched on (the absent member) | IMPL-9 | subclass (a `No*` member) |
| a value cached or decided in a second place (the value's one owner) | IDEN-5 | delete-copy |
| a roster, registry or discovery call (the family's membership) | MEMB-1 | init-subclass, metaclass |
| the old system kept beside its replacement (the one system) | TIME-1, AGENT-1 | delete-copy |
| a test hand-writing internal state (the owner the test should call) | AGENT-3, BOUND-2 | declaration |

The pattern IDs are NRA catalog IDs, the same IDs the formal statement in `judge back` uses.

## 1. Kind dispatch (IMPL-1)

> two kinds of goal action. a parent class for two cases is overengineering
> `if action.kind == "clear":` one more if is fine
> third kind arrives. one more if is fine
> the fourth kind is added in three files and forgotten in the fourth
> the fourth file is the bug
> the family existed from the second kind. every `if` was it, unnamed, in another file
> run it back
> `class ClearGoalAction(GoalAction): def apply(...)`. the fourth kind is one class

## 2. The duck (BOUND-7)

> duck typing is pythonic. ABCs are Java with extra steps
> `if hasattr(widget, "orchestrators"):` wire the plate manager
> it quacks, ship it
> PlateManager and PipelineEditor each hand-write `update_item_list`, `on_selection_changed`, `action_delete`, `action_edit`
> someone renames `orchestrators`. hasattr says False. no error. the wiring just doesn't happen
> it quacked, and it was the wrong duck, and then it was no duck at all, silently
> every `hasattr` was the contract, written at the call site, unnamed
> run it back
> `AbstractManagerWidget(ABC)` declares the hooks; both managers inherit it; `findChild(PlateManagerWidget)` asks for the type
> OpenHCS #44: about 1,000 duplicated lines gone, PlateManager −2,400/+1,200 [23900150]; the silent skip fixed by asking for the class [e8bf6924]

## 3. The service (Theorem 4.6)

> it's a service. services are clean architecture. a service per concern
> 17 service files, one per concern. I merge them into 5 and call it a cleanup [1156efbd]
> `LiveContextService` collects live values from every open form, cached by token
> 11-29: a caching layer, 4–6x faster typing. 12-04: refresh it on open. 12-05: stabilize it
> born 11-28, edited in 11 commits, each one me defending the service
> the morning it died I was lowering its log level [3d59f6cb]
> the live values already had an owner: the model object each form edits
> run it back
> `ObjectState` owns model, history and provenance; forms read it
> `LiveContextService` 364 lines → 0 [1a07ca5d]; the form manager 1,205 → 616 [43a6cba5]. a 364-line service that was one missing owner

## 4. "MI is scary" (Proposition 4.2, Theorem 4.6)

> two capabilities, one class. MI is scary, diamond problem
> AnyIO's `FileReadStream` needs file lifecycle (`_BaseFileStream`) and iteration (`ByteReceiveStream`)
> the writer needs only the first, the buffered reader only the second. neither group contains the other
> one parent it is: inherit `ByteReceiveStream`, forward the file lifecycle
> the forwarder needs `__init__`, `aclose` and `extra_attributes`: 8 ways to keep or omit them
> 7 fail the read/metadata/close client; the eighth passes 3,934 upstream tests
> and the best one-parent design still leaves exactly 1 pair outside, forever (paper1 §1, Fig. 2)
> run it back
> `class FileReadStream(_BaseFileStream, ByteReceiveStream)`. 0 pairs outside, 0 ways to get it wrong
> the scary diamond was one base list

## 5. One procedure, many places (IMPL-12)

> session published? six tests set `sessions.published[sid] = PublishedSession.of(t)`
> it's one line. a method for one line is ceremony
> acquire starts requiring a published session
> the six copies disagree on the key type; four tests fail in setup for a week
> run it back
> `SessionLifecycle.publish(sid, thread)`. one writer; every test calls it

## 6. Threaded through call sites (IMPL-12)

> the snapshot is needed in the leaf. I'll thread it through, it's only seven call sites
> seven signatures, seven forwards, one of them passes a stale one
> the stale one is the bug, and the fix needs eight edits
> run it back
> the leaf's owner already holds the snapshot. one property on the base; zero parameters

## 7. Identity as a string (MEMB-4, IDEN-3)

> execution id. it's just a string
> `if run_id.startswith("exec-")`
> a thread named `exec-7` gets an execution's reply
> I built a type checker out of string methods
> run it back
> `ExecutionId`. it can't equal a thread name, because it isn't the same type

## 8. None branching (IMPL-9)

> no execution yet? `None`. simple
> `if run is not None:` here, `if run:` there
> the empty string walks in. two files disagree about whether anything runs
> run it back
> `NoCurrentExecution`, a member with methods. nobody asks; everybody calls

## 9. The second cache (IDEN-5)

> a cache here is faster
> receipts cached per visible set, re-read fragments default to "Checking"
> two caches. every scroll draws "Checking", then the answer, then re-anchors
> run it back
> one answer per message, owned once; a re-read starts from it and nothing redraws

> each flashing row needs a colour. store it per row, update it every 16 ms. explicit is better than implicit
> N rows, N colours computed and stored 60 times a second, N repaints
> the colour was a function of one number: when the flash started
> run it back
> store the start time; paint computes the colour from the clock. N colour writes per tick → one overlay repaint per window [OpenHCS #58, 6235496b]

> the sticky header needs to know when to pin. three places measure it
> a two-row header pins one row late, drawn one cell to the left
> run it back
> `sticky_header(block, row)`. one decision; the pinned copy draws the flow's own layout

## 10. The registry (MEMB-1)

> five registries. explicit is better than implicit: each gets its own metaclass, a `RegistryConfig` and a `discover_all_*()` called by hand wherever one is read
> the storage backends alone: 96 lines of configuration before the first entry
> run it back
> `class StorageBackend(metaclass=AutoRegisterMeta): __registry_key__ = "_backend_type"`
> members register through the MRO; the registry discovers itself on first access
> that registry 96 lines → 10; five custom metaclasses → 0 [OpenHCS #45, 16f974c7, 04475627]

## 11. The second system (TIME-1)

> the new compiler works. keep the old one for compatibility. you never know
> two compilers, two runtimes, a benchmark library of 92 per-module function shims beside them
> two answers to what one pipeline means. Theorem 4.7: independently edited copies disagree
> run it back
> delete the parallel lattice outright: 41,257 lines out in one commit [OpenHCS #60, 5e8812ee]

## 12. The test that writes the internals (AGENT-3, BOUND-2)

> the fixture is slow to build. I'll poke the private field directly
> green
> the product changes how that field is filled; my test still pokes the old shape
> it's green on a state the product can't reach
> run it back
> the test calls the owner that fills it. one fixture owns "a bound, running session"

## What good judgment looked like

Five OpenHCS PRs (agent-comms `docs/refactor/cleanup-20260929/OPENHCS-HISTORY.md`). Each deleted a copy by declaring its owner, and none paid for it with a layer.

- **#44**, probing and two hand-copied managers → one ABC with declared hooks. subclass, base-method; §2.
- **#45**, hand-written discovery and a metaclass per registry → `AutoRegisterMeta` reading the base's declaration. init-subclass, metaclass; §10.
- **#51**, every open window refreshed on any change → `issubclass` against the changed type selects the windows that inherit it: about 200 ms → under 10 ms [e19a962f]. The MRO already knew who cared. declaration.
- **#58**, a service and per-row state beside the model → `ObjectState` owns the model and paint derives from time. delete-copy; §3, §9.
- **#60**, a parallel compiler and runtime → deleted outright. delete-copy; §11.
