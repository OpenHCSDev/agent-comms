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

Implement the shared owner and its callers first. Final validation must exercise
the normal configured saved-session native startup using a matching native
artifact, compare the installed context/count with the original selected source,
and verify that over-budget history remains uninstalled. No public input,
provider retry, original UNKNOWN replay, or public owner restart is authorized.
Mendel separately owns saved385 post-native completion and reply publication.
