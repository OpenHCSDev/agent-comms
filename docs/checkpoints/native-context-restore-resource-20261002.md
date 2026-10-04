# Native context restoration resource

Base: merged main `dd79dabc` (#532). Owner: Arendt. Native builder: Schrodinger.

## Source finding and change

`SessionContext.restore` currently decodes the selected history in `sourceBudget`.
When that context is admissible, `ReadyContext.install` decodes the same history
again. The SDK's initial settings lookup reads selector metadata, so it is not
a third full body decode.

The existing `SessionContext` owns restoration. It acquires the selected
messages once, pass that resource to the existing `ContextBudget`, and install
those same messages only when admission selects `ReadyContext`. A compaction
context retains its existing empty resident messages. Later budget decisions
still acquire the current selected source; no evidence or budget cache is added.

Consumers: native constructor; native session replacement; native compaction
completion; native session switch/fork; `NativeCompactionPolicy`'s current-context
check; ready/compaction message projections. `EntryStore` keeps branch selection
and source revision checks. `ContextBudget` keeps estimation and admission.

## Qualification boundary

Saved385 evidence reports about 1.9–2.6 seconds in each initial `get_state` receive.
That interval includes native initialization and is not a measurement of either
history decode. Historical372's 18/20-second timeouts remain unallocated. This
change removes a source-proven duplicate operation; it does not establish a
whole-turn latency cause or provider capacity claim.

## Completed receiving qualification

Functional source: `4f5823db6dca2b0a948157b5c4359873b8bf4bfe`.
Matching artifact: manifest
`00c2c11feaaf6e53b543d1570ae6029f8395e64ebafcdac54c352082cbc460c7`, tree
`e0f93fec172aced120c0ce3e90345a27ece91ac2a0436bad3353169922dfecc7`.
Only compiled `dist/core/session-context.js` differs from qualified532; all other
compiled files are identical. Production change is one authored module,
16 additions /15 deletions. The second selected-source decoding algorithm in
`ReadyContext.install` is deleted. No Python production or runtime format changes.

One actual native CLI/RPC read opened the same 42,924,971-byte saved fork used
for532, with all four automatic global extensions. It returned27 messages in
**2.317254 seconds** and retired its child. The earlier single532 observation was
2.393997 seconds. These are individual observations, not a controlled performance
benchmark. The returned historical model was `openrouter/moonshotai/kimi-k2.6`,
thinking `high`; no provider request was made. This was saved-state startup,
not a configured Sol prompt, physical UI journey, or concurrent cohort.

The unchanged original projection matched the installed ready messages.
Restoration observed27 entry-body reads, one per selected entry; the selector
count used zero body reads. Over-budget selection left resident messages empty.
Existing branch/compaction-kept-suffix/source-revision controls passed in the
same final batch. Source and fixture-settings hashes were identical before and
after. The immutable original42MB donor and adjacent input-proof journal were
unchanged. Network was kernel-denied. No input, original retry, public mutation,
or provider prompt occurred. Child cleanup and a final owned-node process scan
both reported no surviving fixture child.

The existing Prettier TypeScript parser parsed485 installed native files:
the entire coding-agent `dist` JavaScript/MJS/declaration root, plus the original
budget and estimator boundary. It found exactly the three existing context
classes, two imports, and19 relevant calls; no parse omissions. The receipt
records declarations, method signatures, imports, call argument counts, source
hashes, parser identity, and limits on dynamic resolution. The original nine
Python startup/custody/channel owner files also parsed without omissions. These
are source references, not a claim of runtime dispatch resolution or a census
of every external dependency.

Receipts: `evidence/native-context-restore-resource-20261002/`.
Raw owned fixture: `.artifacts/533-native-read01/`. Mendel separately owns
saved385 post-native completion and reply publication. Original372's18/20-second
timeouts and total user-turn latency remain open. There is no optional S1 policy
activation, changed budget decision, or readiness timeout adjustment.
