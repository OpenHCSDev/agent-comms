# Ordinary-turn claims and Toad cancellation

Owner: pi-toad-cancel. Branch: fix/toad-cancel. Not for merge until real journey results below.

## Source decision

The nine blocked source claims on pi-cn-p3 were ordinary acquisitions (bus sequences 1561–1566 and 1568–1570), all with `claim_scope=None`. `OwnedSend.claim_resource` published them; only `RecoverySnapshot.release_resources` retired selected-attempt claims. This was a missing ordinary resource lifetime, not a selected UNKNOWN settlement failure.

`OwnedTurn.stream` now closes its real backend stream before retiring the acquisitions held by its `OwnedSend`, including cancellation and failure. `ClaimWrites.release_resources` owns the shared bounded exact-acquisition release; selected recovery consumes it too. No schema, timer, owner-name comparison or retry is added. Abrupt process death before ordinary cleanup is not proved by this change; those unbound acquisitions lack a durable ordinary-turn identity.

AST source census used the existing debt-ratchet `ChangedSource.tree` over all 662 Core and installed Toad Python modules. Zero parse omissions. Declaration/call/write sites retained in `/home/ts/.cache/agent-scratch/pi-toad-cancel/ast-before.txt`. Dynamic dispatch is source evidence, not a proven runtime caller. NRA could not import against installed metaclass-registry, and its own Python 3.12 cannot parse this project's 3.14 syntax; no NRA completion claimed.

## Incident evidence

Toad log `~/.local/state/toad/logs/Agent_Comms_2026-10-19T13_08_20_015597.txt` lines 3–10 shows session/load, its successful response, then an outgoing session/cancel before any client prompt. Installed Toad's only production RPC cancel sender is `AgentController._cancel_prompt`, reached from the Cancel action or `/cancel`. Attach/load has no call to it. The log does not record physical keys, so the human/UI trigger remains unproved.

## Live repair

Released all nine original stale acquisitions using the branch's `ClaimWrites.release_resources` with their exact original ClaimOwner witnesses under a real NativeBoundary. No database edits. Verified none of those nine generations remains. Left the newer selected tests/test_importing.py claim intact.

pi-cn-p3 confirmed at bus sequence 1606: `functions.edit` successfully changed cli_bridges/history.py (two replacements), no shell bypass, and its current test-file work remained intact.

## Scratch ownership

`/home/ts/.cache/agent-scratch/pi-toad-cancel`: AST evidence and bounded real-process acceptance output, owned by pi-toad-cancel. Preserve any uncertain input/session disposition. Retire disposable runtime copies after the run and keep concise results.

## Checklist

- [x] Inspect incident ACP wire and exact claim projection.
- [x] Implement ordinary resource retirement and share exact-generation release.
- [x] Release all nine original claims and obtain real history.py edit confirmation.
- [ ] Reproduce attach during a real 30-second Pi bash operation on main and branch.
- [ ] Confirm success, failure and cancellation release through the real native path.
- [ ] Focused checks and standalone debt ratchet.
- [ ] Record remaining trigger uncertainty accurately; do not manufacture an attach failure.
