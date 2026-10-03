# S4 private native source projections

Use the original EntryStore for uncompacted ancestry and its existing kept cut.
SessionContext owns entry-to-message conversion; TurnContext owns measured SDK
context construction, source matching and rendering. Extend these owners and
share their current algorithms. No production selector, commit, runtime store,
model call or optional timing change.

Full context is exact uncompacted ancestry and is eligible only when the native
budget admits it. Recent-only is the original policy-owned kept source. Canonical
task-memory retains its existing owned payload. Bounded needs an original
narrative-only summary plus the same kept source; do not strip a packed stored
summary or invent an absent raw provider response. A new generation remains an
external run requirement, not a prerequisite for the source projection code.

Private projected inputs must preserve original source/revision/provenance and
remain distinct from an actual submitted SDK/HTTP request. Labels cannot grant
application, budget, recall or constraint permission. The comparative study is
still unapproved. Mendel owns launch/reservation/lifecycle, Sch compiled artifacts.

## Source change in #589

`EntryStore.keptMetadata` now owns the existing kept-floor walk used by both
canonical `contextMetadata` and the private recent view. `uncompactedMetadata`
walks the same original ancestry without summary entries. No second scanner or
entry store is introduced. The old inline kept walk is deleted.

`SessionContext.entryMessages/entryContext` share original SDK entry conversion.
`TurnContext.project` resolves original IDs through the acquired store and uses
those same entries for conversion and provenance. It checks the original store
around the awaited projection. Normal capture/next and context installation keep
their original source. Preview provenance does not grant admission or submission.

`tests/retained_native_conditions.mjs` consumes these views and original
`ContextBudget` for private construction. The existing SDK source contract calls
it with `--source-projections`; no model prompt is sent. Missing model metadata
leaves budget unavailable. Missing uncombined narrative leaves bounded unavailable.
The scorer's submitted-input and comparison claims remain unavailable for previews.

Built-in Acorn 8.18.0 parsed the immutable pre-change stack/test roots (46 modules),
the changed roots (47), and frozen native2b coding-agent distribution (268), with
zero JavaScript parse omissions. Each source root has one declaration of
EntryStore, SessionContext and TurnContext. SDK callers still use contextEntries,
buildContextEntries, capture and next; private full/recent calls use project.
Member calls are lexical evidence, not proof of dynamic resolution. TypeScript
declarations were read separately: existing TurnContext runtime methods are not
declared in turn-context.d.ts; encoded context/event interfaces do not change.

Working source is published for review. Matching native compilation and the
affected SDK source construction check are still pending; this is not Ready.
