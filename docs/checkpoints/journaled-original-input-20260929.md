# Original input admission and native compaction

Owner: parent Codex thread. Status: implementation and installed-path verification.

## Reproducer

The live `openhcs-helper` autonomous goal continuation reached native Pi without
an original input reservation. `OwnedTurn.prepare_native` therefore skipped the
journaled compaction mechanism. Stored native context rejected the prompt before
its user-message start. The failed goal attempt remains preserved and blocked.

## Ownership and scope

`OriginalTurnInput` owns eligibility for original-input compaction, inherited by
owner, routed and dependency inputs. `InputDispositions` remains the only durable
input reservation/binding authority. A scheduled input without bus/ACP ingress
gets a reservation derived from its admitted turn identity. Goal launch authority
remains in the existing goal attempt store; a reservation never authorizes a goal.

This removes the owner-only compaction override (IMPL-4, BOUND-2), and stops using
absence of a durable key as a proxy for autonomous input (IDEN-1). No second goal
compaction mechanism, journal, reader, codec or retry path is introduced.

## Acceptance

Use the existing saved-native/ACP goal journey and localhost provider: restore
context that requires compaction, schedule one autonomous continuation, observe
journaled compaction before one native user start, then verify summary and input
proofs, successful goal settlement, and preservation of unrelated uncertain input.
Run this journey against an installed wheel. Check the current baseline fails the
same journey. No user-thread input replay or goal resumption is part of this fix.

Results and deleted lines will be recorded before merge.
