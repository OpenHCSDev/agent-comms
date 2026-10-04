# S4: Original SDK full-history selection and admission

## Working scope

The SDK `EntryStore.uncompactedMetadata` owns the original full-history entry
selection. The private constructor currently retains its chosen entries but
drops that original full-history reference. Python's message-entry counts and
the constructor label cannot replace the SDK's selection.

Retain that original selection as `JournalProvenance` at the same acquired
`NativeWitness`. All constructors, installation observations, original probe
readers and scorer consumers use the existing owners. `RecordedConditionInstallation`
validates both original selections against the acquired ancestry and derives
their equality. Delete the message-count completeness flag.

Join this selection to the complete constructor/request prefix and the original
request's `ContextBudget` admission. Report SDK-estimated full-history admission
separately from actual provider-token capacity, HTTP bytes and a matched study.
Missing historical selections remain unavailable; no reconstructed evidence.

Before-edit Python AST: existing refactor-audit parser over src/tests/tools;
raw `.artifacts/s4-full-history-admission/owner-before.json`. SDK entry selection,
SessionContext conversion and budget semantics read directly in original stack
source. Lexical sites do not prove dynamic resolution.

Source implementation is in progress. Final checks follow the complete batch.
No SDK/artifact/holder execution, original session reads, native/runtime edits,
package/environment operations or provider inputs. The 30-pair/USD75 study is
unapproved. Full S4 remains unfinished.
