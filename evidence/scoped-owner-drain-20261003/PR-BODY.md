## Changes

Delete72/add138 lines in seven production files. InputDrain observes its existing
owner, participant/pointer, receipt-backed pending work and cursor instead of
shared file mtimes. The declared PendingNotification capability selects pending
SQL rows before decoding settled history. CursorOwner validates the exact returned
durable row against the same operation's original coverage; ACP/publication consume
one verified refresh instead of rereading native history.

No new cache/store/counter, protocol, native bundle or schema. Original admission,
UNKNOWN, live owner, source identity, recovery and publication ordering remain.

## Qualification

- Seven affected real private state controls pass in3.27s: quiescence, own recovery,
  unrelated owner writes, genuine append/pending work, replaced owner and original
  sealed receipt/read behavior. Initial fixture mistakes remain disclosed.
- Normal existing69-dependency installed holder;339 source files and three declared
  resources match f476007e, pip check passes. Actual accepted549 saved native proof
  verifies; exact unchanged SQL row returns, unproved prefix and borrowed owner
  admission refuse. All27 original/proof files unchanged, zero provider/input.
- Existing refactor-audit AST before/after covers1002 Core/Toad modules, zero parse
  omissions; dynamic resolution ambiguity is explicit.

`evidence/scoped-owner-drain-20261003/INSTALLED-ACCEPTANCE.md` and raw receipts
contain scope, paths and hashes. Production source is unchanged from reviewed
d0eae668. No new ACP/native turn, UI or public latency acceptance is claimed.

## Remaining latency

Original469 boundaries managed compaction committed at+144.265s, before TRIAGE
preparing+146.666s. Its relevance/answer requests span7.305s. This patch removes
repeated owner/history work; it does not claim to fix that compaction preparation
delay. Parent's next original configured channel wave supplies user acceptance.
Inherited551 keeps its separately reviewed quiet checkpoint rebuild requirement.
