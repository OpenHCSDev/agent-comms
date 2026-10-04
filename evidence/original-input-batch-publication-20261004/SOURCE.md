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

Implemented production: three existing files, 46 added / 30 deleted. Removed
per-member document publication, per-member wire metadata read, capture's store
read, the retained `OwnedTurn.key`/`snapshot` fields, and the separate scheduled
reservation callback procedure. All five capture callers consume the required
original document. Queued ACP and fork scalar recording keep their contract via
the same publication operation; scheduled reservation still rejects duplicates.
No schema change, new class or new semantic None-state. The pre-existing optional
custody argument is resource presence; absent sequence is the original ACP or
scheduled source contract, not an input disposition.

Custody: `InputDocument.record` admits original membership once. The store derives
new keys from the returned document delta, enlisting only those keys before
publication. Old reservations and native UNKNOWN are not added to another
caller's rollback. `OwnedTurn` already registers its async original-input
retirement before acquiring the wire cut, and now records all selected keys
before the one publication. No native input has started at that boundary.
Fresh native binding, start, compaction and recovery checks remain independent
operations on their original authorities.

Installed qualification: `d4d5f6b8` production, normal wheel `aa1ec28e...`, all
342 installed members equal; ten installed distributions/SDK 0.12.1, pip check
passes. Existing thin540 exclusive grant from Bohr, no new environment or source
overlay. Accepted immutable native086 read grant, no native build/copy/change.
Resource check showed 7.1 GiB RAM available, 11.2 GiB home and 18.8 GiB swap;
one small serial batch reused the holder and only one saved-native child.

`installed01.log`: actual saved SDK owner and channel identity/capture passed;
two new document controls failed on authored invalid key/sequence pairs before
publication. Raw negative retained. Production stayed unchanged. The two fixture
inputs were corrected to the existing direct ACP contract and only those two
controls rerun: `installed02.log`, 2 passed / 0.20 seconds. No native repeat.

Four changed contracts are qualified: prior rows/UNKNOWN cannot be replaced;
batch and scheduled rollback are enlisted before a lost publication ACK;
duplicate scheduled reservation remains strict and no-op publication preserves
bytes; exact channel ordering/prompt and actual saved SDK direct/channel/scheduled
originals reach context and retirement. The latter asserts source bytes unchanged,
zero provider posts/native starts, canonical idle lease, released input resources,
native children absent and owned groups empty. Not a provider, live UI or
historical 13/98-second improvement claim.
