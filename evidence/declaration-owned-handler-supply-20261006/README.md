# Handler supply belongs to the consumer declaration

`MroDispatch.handlers_for` rebuilt every effective consumer method and probed
handler annotations for each value-MRO capability on every publication. The
original right-sidebar profile spent about12ms own/21ms inclusive there.

The same class now selects its effective handler declarations once in
`__init_subclass__`, after cooperative superclass initialization. Unannotated
C3 overrides suppress inherited declarations. Value capabilities still run in
original MRO order; methods still bind on the original instance at invocation.
All async/sync/replacement and consumer resource hooks stay unchanged. No event
cache, alternate registry, new lifecycle or second dispatch path.

Existing Package AST: Core324production/378tests/53tools; Toad288production/
403tests/40tools; native249production. No parse omissions. Tracked membership
writes are exclusively the original `handles` decorator. Tests intercept
`handlers_for` and instance behavior; neither API changes. Arbitrary external
runtime class/marker mutation is unresolved by lexical source analysis and is
not a tracked registration API. Dynamically declared subclasses still acquire
their own supply through the original subclass lifetime.

Toad CoreEventReceiver/MroProjection and Core event, request, turn, goal,
failure, tracked-turn and AST-ratchet consumers all share this implementation.
Original Toad/native subclass hooks are cooperative. No shared-file conflict
was found; SQLite lifetime work is separate.

Working source checkpoint; final declaration controls and affected source App
confirmation follow the coherent implementation. No installed/live claim.
