# Original input batch publication

Base: `8f16339673835114f526d7431032576099764af5` (#615 merged).
Owner: Mendel. Existing checkout, original input publication/capture only.

`OwnedTurn.begin` publishes and decodes the input document separately for each
channel original, reads wire metadata per member, then `InputBatch.capture`
reopens the document. These are repeated operations on the same admitted cut.

Use existing `InputDocument.record` for all original members in one publication;
`InputDispositions` returns that exact document and enlists supplied rollback
before publishing it. Single recording and scheduled reservation use the same
operation. `InputBatch.capture` consumes the decoded document; it does not open
storage. The channel producer acquires wire metadata and registry once under its
original wire lock. Existing row membership owns refusal; original native
binding/start admission stays fresh and separate.

Before AST: `before.json`, existing refactor-audit `Package.load`, production,
tests and tools roots (311/364/53 modules, zero parse omissions). Generic
`record`/`capture` references are lexical leads, not dynamic resolution proof;
the complete original-input imports and declarations were read semantically.
Only one production `InputBatch.capture` caller and four fixture calls exist.
Scalar `record` producers (queued ACP and initial fork input) retain their
existing contracts through the same publication owner. Scheduled reservation
retains strict duplicate rejection and callback-before-publication custody.

Catalog: BOUND-1/BOUND-2 (decode once, consume the original owner), IMPL-12
(one original publication/custody procedure). No new class, cache, registry,
codec, native artifact or inspection claim. Input document wire format and
row disposition hooks remain unchanged. UNKNOWN and prior originals must never
be overwritten, retired by another reservation, or replayed.

Final qualification will exercise only the changed grouped/single/scheduled
capture, duplicate/custody/refusal contracts and the existing actual saved SDK
owner path, installed in an explicitly released holder. No provider input or
historical 13/98-second improvement is claimed. No holder loan is assumed.
