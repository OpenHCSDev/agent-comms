# P387: bridge acknowledgement before confirmation

Source before changes: `810cde3d0`, `src/agent_comms/cli_protocol.py:134`,
`cli_bridges/shared.py:267`, and `messaging.py:174`.

## Requested copied-bus reproduction

On 2026-10-21, the live unread claude-ai → claude-code count was **434**;
the first reference was `(741, 3ae0a22724fe)`. No live message was acknowledged.
The existing practice-copy owner copied 1,927,951,774 bytes in 50.07 seconds;
the bridge SQLite state was copied through the existing SQLite backup owner.
The private copy's read-ledger identity was rebound to the copied wire inode,
only after proving that it matched the original wire inode. Message read facts
were retained, not reset. The copied binding's root alone was retargeted.

| Fact | Observed value |
|---|---|
| Stored bridge outcome | delivered |
| Binding recipient | claude-code |
| Core canonical recipient | claude-code |
| Stored payload reference | 741 / 3ae0a22724fe |
| Decoded Message reference | 741 / 3ae0a22724fe |
| Core unread reference | 741 / 3ae0a22724fe |
| FilteredOutcome's call to Messaging.acknowledge | one call on the private copy |
| Count returned by Messaging.acknowledge | **1** |

The final copied-inbox read met `RouteIndexRebuilding` after the copied index
had prepared 4,195,166 of 27,171,448 bytes. That is not an acknowledgement
failure; the complete probe did not finish. The count above came from tracing
the actual acknowledgement return in its worker thread, without replacing a
Core function. The historical reason Core lost these read facts is not proved.
The current name/reference mismatch proposed in the order is not reproduced.

## Source relationship

`DeliveryRow.finish` persists its final outcome before awaiting
`Outcome.acknowledge`. `FilteredOutcome` discards the read owner's count.
Consequently an exception or partial/no-op acknowledgement can leave the
bridge's independently stored final state ahead of Core. IDEN-5: two writable
answers to whether Core confirmed delivery. Confirmation must follow the
canonical acknowledgement, not precede it. Pending emission evidence remains
pending when acknowledgement fails; no old message is replayed or backfilled.

Refactor-audit parsed all Python source roots before implementation. The full
owner/import/consumer evidence and minimal reproduction log are retained in
`/home/ts/.cache/agent-scratch/fork-external/P387-before-source.txt` and
`P387-reproduction.txt`. Dynamic receiver spellings are not a static dispatch
proof. SQLite and Claude wire contracts were read semantically.

## Repair and copied application result

`Messaging.acknowledge` rejects a short display basis before writing any reads;
`MessageAcknowledgmentError` carries the recipient and unmatched references.
`DisplayBasis.uncovered` derives those references from its captured positions;
the bus has already checked the original sequence/id pairs. Invalid original
references also propagate as that declared acknowledgement error.

`Outcome.finish` now owns acknowledgement followed by the SQLite confirmation.
The already-confirmed `FilteredOutcome` does nothing on a later observation,
including the real race where transcript confirmation precedes socket drain.
The replaced `followed_by` methods and duplicate-message re-acknowledgement are
deleted. Both monitor siblings consume this shared outcome behavior.

The monitor fixture uses a real practice-copied bus instead of a fake Core.
Both task-record formats still test split, out-of-order, duplicate and reopened
history; the failed ACK runs through the real bridge error logger and remains
emitted, not confirmed. Partial batches change no canonical reads.

Targeted result before publication: **67 passed in 7.11 seconds**. The retained
full-bus copy also completed a fresh successful notification: Core reported it
read and the bridge stored delivered. A separately fresh, wrongly routed
notification raised the declared error, retained emitted and left reads
unchanged. Both runs use the bridge's actual line encoder; an earlier probe's
incorrect generic JSON encoding was rejected as foreign input and is retained
as a failed probe, never retried. No historical or live backlog was replayed.
