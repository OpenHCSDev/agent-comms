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

## Native and structural results

Installed wheel, real pinned native776, localhost provider: the new cold-saved
goal journey passes. Exactly one journaled compaction precedes exactly one new
native user start; summary progress/publication crosses ACP; goal generation
advances after native completion. Saved bytes and an unrelated reserved input
remain intact. The baseline fails the identical journey with a blocked generation.

The existing failed-goal/retry journey also passes: provider failure is published
once through the typed receipt, the failed attempt remains blocked, explicit Retry
dispatches one fresh continuation, and the older uncertain input is preserved.
Ordinary owner fences/flock and input-disposition checks pass. Seven ownership
guards pass, including a new declaration-derived guard that every original case
inherits reservation and compaction. The packaged ratchet reports no growth;
one long chain and seven boolean terms were removed. Eighteen production lines
were deleted relative to current main (including the owner-only override).

The broader run exposed eight failures in the older adaptive test file: obsolete
nullable row fields/status assumptions and synthetic native history rejected by
current preparation. Mendel owns replacing that fixture with the existing real
native fixture; those failures are recorded, not a claim of a green full suite.

Toad204/205/201 are merged: same-open-view inbound replay, cancellation feedback,
and reusable video/profile custody checks. The staged pair uses Core2f9f0f08,
Toad8616aee5, Textual412b and unchanged native776. Arendt owns actual autonomous
compaction/summary painting acceptance in this exact staged installation.

Owned disposable scratch: `/home/ts/.cache/agent-scratch/comms-journaled-goal-input-20260929`;
staged install: `/home/ts/.local/share/agent-comms/runtime-journaled-original-20260929`.
The active bus, original failed goal/input, native sessions and running owners
have not been reset, resumed, replayed or restarted for this candidate.
