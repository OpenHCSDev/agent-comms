# Captured original input lifetime

Base: d7d61b12de2be0e730e9474efdaf48aac2692ba1 (main including 608/611/613).
Owner: Mendel. Same existing goal-ledger checkout, no new environment/native.

## Owner and complete consumer relation

InputDocument.originals is the sole ordered-membership/duplicate/missing receipt
boundary. InputBatch.capture captures those StoredInput objects and derives
channel admissibility from exact distinct original messages, ordered keys and
original text. OriginalTurnInput retains the admitted batch. StoredInput owns
context_provenance; TurnContext.for_owner derives user-segment provenance from
these original rows, distinct from original wire references and owner identity.

OwnedTurn.begin previously captured the batch, then prepare_prompt opened and
decoded InputDispositions again and reselected identical keys. It also retained
self.batch after OriginalTurnInput became the retained owner. The same local
InputBatch now supplies prompt assembly and reservation; the second read and
extra retained field are deleted. Channel, ACP/direct, dependency and scheduled
originals share this path. A scheduled turn captures no external originals before
rendering, then reserve_turn returns its original input from the exact changed
document with rollback already enlisted. It cannot add itself as provenance of
the prompt used to create itself.

Original keys remain the acquisition/rollback coordinates: bus records enlist
before capture; reservation transfers into outer input custody before worker
return. No native binding, uncertainty, retry, goal, lease or record operation is
changed. Fresh input checks in compaction_boundary/selected_source and public
InputProvenance.require_original resolve current certainty/custody; they are
separate observations, not this capture, and must stay fresh.

## Source evidence and scope

Before/after use existing NRA/refactor-audit Package.load over src/tests/tools;
lexical declarations/imports/attributes/decisions are source leads, not dynamic
resolution proof. Before: 311/364/53 modules, zero parse omissions. Full source
read confirms only OwnedTurn.begin calls prepare_prompt/reserve_input; the
failure-injection test accepts arbitrary arguments. InputDocument.reserve_turn
and OriginalTurnInput.reserve already return original captured rows.

BOUND-1/BOUND-2: consume the already captured typed source instead of decoding
and looking up its membership again. No new type, cache, store, codec, hook,
semantic flag or compatibility path. New batch cases still enter through the
existing InputBatch and StoredInput behavior.

Final changed acceptance will exercise original SDK saved owner/input capture,
context provenance, single/channel/scheduled reservation and custody cleanup in
an existing explicitly released holder, no provider prompt. Production and
original private evidence remain distinct. This is a source-proven redundant
read removal, not attribution or a speed claim for historical 13s/98s spans.
