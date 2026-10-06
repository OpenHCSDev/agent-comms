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

Final checks:3 existing ordering/replacement variants passed; added C3 masking/
instance-binding control passed1/.78s after correcting its authored abstract
constructor. No production adjustment resulted from that refusal. The system
author environment had pytest without ACP; the App environment had ACP without
pytest. Startup refusals remain. Source controls reused the App dependency order
and appended installed system pytest dependencies; no package/env changes.

ONE changed source App run completed0/empty stderr:508widgets/10tabs/zero
provider. Core baseline product files d8467b730 and original574098293 are Git
byte-equal; only the dispatch owner changed. Toad437e/native79 unchanged. Right
profile36 handler calls fell from~21ms inclusive to0.108ms; left53 calls took
0.166ms. Median first headless display36.0ms(left)/36.5ms(right), versus the most
matched native79 run41.8/47.4ms. Single-run source result, not installed/live
latency or terminal pixels. Width-dependent layout/render remains unchanged.

RESULT.json pins original/changed profiles and all control/App logs. All launch
handles are terminal and fixture temporary roots are empty. No installed pin,
public runtime, artifact, provider or original session changed.
