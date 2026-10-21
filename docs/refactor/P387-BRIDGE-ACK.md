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
