# Retained-child selected turn settlement — default-OFF design, not activation

**Status: isolated test-only executable P0 prototype / NO-GO.** Fresh-root schema/snapshot v3 deliberately refuses existing v2 roots; PR116 alone owns migration. `retained_turn_settlement.py` has no ACP/Pi caller and requires an in-memory `_test_retained_turns` flag, which resets OFF on reconnect. Synthetic tests exercise the same SQL/response gateway, not actual Pi terminal evidence. This is not production authorization. Addresses P0 of the independent exact-`a894300` review (`/var/tmp/pr94-selected-adoption-independent-review-a894300.md`). The executable negative at `87932d1` demonstrates that `backend_done=True, process_dead=False` cannot pass today's real bus/SQL `prepare_fenced_response` gate. Do not write `process_dead=True` for a living Pi child. This proposal is neither PR116 migration authority nor a positive ACP callback. No provider/live use or UNKNOWN replay.

## Truthful facts and ownership

An idle ACP controller, holding the approved outer session-writer fence and local child lock, claims **one new** canonical input-bound turn. It never borrows a running human/ACP turn. The exact claim binds recipient/owner generation, owner task and goal, session directory+inode/file+proof journal, Pi child identity, ACP controller, registry turn ID/generation, sealed source and FULL claim/revision, execution/attempt fence and a single native input ID. Child identity and tool capability need separately reviewed in-child guards; this design cannot manufacture either.

`process_dead` continues to mean **the child actually terminated**. Define a separate typed `turn_settled` receipt: the *same child* has finished **this input's** model/tool turn while remaining alive. It is not `backend_done`, a projected `tool_execution_start`, a synthetic RPC message, a UI final chunk, EOF, a timeout, or a parent-observed idle state. Its authority must arise at an independently reviewed in-child terminal boundary, after all tool effects/outputs have settled, with exact input ID/nonce and controller+session+turn+attempt identity, final event identity, terminal stats, capability generation, and an ordered child event sequence. Conditional source admission and the per-tool pre-effect claim guard are separate prerequisites. A terminal error/abort cannot be recast as successful settlement.

For production, the child receipt must be committed durably to the existing selected input journal and corroborated by private saved-session input/context records before SQL marks settlement; the source/journal/SQL transitions must use one reviewed raw-first ordering and a crash-recoverable intermediate state. The SQL row is bound to the existing **same** execution/attempt/input and owner claims, not a new SQL authority. No receipt from another attempt, child, session revision, turn, input, or capability generation may settle it. A child process death after a committed valid receipt is a separate lifecycle fact; it does not retroactively fake or invalidate the prior turn's settlement. A later ordinary prompt in that same child is a required acceptance check.

## Schema/API boundary for PR116 owner review

The PR116 migration owner alone chooses migration and DDL. Proposed *semantic* shape:

- A discriminant for selected persistent attempts, defaulting existing attempts to `one_shot`; its admission requires an enrolled first-source saved session and shared input-ID FK/journal exclusion. It is immutable once source admission starts. No second journal/claim table or recipient-wide path fallback.
- A unique attempt/input-bound `turn_settled` evidence record with immutable owner/session/child/turn/claim/attempt/profile identity and durable receipt references. CAS insertion requires current FULL source/claim, owner turn, attempt, input and raw marker; duplicate **identical** receipt is idempotent, conflicting receipt is UNKNOWN/blocked. A boolean set by the parent is not evidence.
- A typed terminal predicate `one_shot => backend_done AND process_dead`, `persistent_selected => committed turn_settled AND backend_done`, including SQLite CHECKs/triggers, Python `AttemptRecord`/transition validation, response preparation/publication, nonpublication settlement, recovery and projection. `process_dead` remains false for a live child. Do not broaden the legacy death gate globally, nor allow a one-shot execution to change type to bypass it.
- The same existing bus-keyed publication intent and exact-route response gateway must CAS the persistent receipt + current owner/claim/turn/attempt/input at prepare, publication and recovery. ACK/cursor advances only upon the durable exact bus receipt. No separate ACP response publisher and no legacy ACK fallback.

The isolated PR94 **fresh-root prototype** changes the SQL terminal CHECK, Python attempt model, response gateway and replay together and has provider-free tests. It does not install/migrate v2 roots, link PR95 raw-first input FK/journal or authenticate in-child receipt provenance. Therefore its test-only gate must not be opened in ACP or shipped as a production migration. PR116 migration ownership and independently reviewed integration remain prerequisites.

## Crash, timeout and duplicate disposition

| Boundary | Durable disposition | Permitted action |
| --- | --- | --- |
| Before reserved source/input and any raw write | no execution/receipt; no inferred STARTED | deny or retry only under a **new** explicitly authorized admission; never reuse an uncertain reservation |
| Reservation/UNKNOWN marker committed; raw byte not proven absent | UNKNOWN, input ID held | no resend, new ID, child replacement or response |
| Raw input possibly sent; no exact committed in-child terminal receipt | UNKNOWN | retire/fence child on uncertainty; no response or replay, even if parent saw final text/stats |
| Exact terminal receipt durably committed but SQL settlement interrupted | recovery-pending, not permission to prompt again | under same owner/session/input fences verify immutable journal/child evidence and CAS the *same* receipt once; mismatch remains UNKNOWN |
| SQL settled, response intent not yet durable | settled, unpublished | prepare exact-route intent only under fresh current fence; if owner/claim changed, block for recovery (no alternate recipient) |
| Intent durable, bus append/ack interrupted | publication uncertain | query keyed bus receipt; resolve idempotently to one response or remain UNKNOWN; never append a second different response |
| Bus receipt durable but ACP disconnect or child dies | committed response, current child state independent | reconcile same keyed receipt/cursor once; no retransmission or reuse of dead child |

A timeout, cancellation, socket close, child crash, duplicate controller, or ambiguous receipt is **not** evidence of settlement or permission for another raw send. For a durable exact receipt, recovery may complete only *disposition/publication*, not model execution. Never reissue a prompt to resolve uncertainty. Explicitly separate an actually proven zero-byte denial from a possibly written input; neither should be silently promoted to success.

## Required negative/positive offline proof before any positive callback

1. Retained child with `backend_done=True, process_dead=False` must fail the existing one-shot gateway with zero bus/obligation mutation (`tests/test_coordination_response.py::test_retained_child_cannot_publish_under_one_shot_process_death_contract`, committed `87932d1`); unchanged one-shot positive still passes.
2. Wrong input/session/inode/controller/owner/goal/turn/claim/attempt, changed tool generation, absent terminal tool completion or forged/duplicate conflicting receipt: cannot write settlement or response; no raw replay.
3. Inject crash/timeout at every marker, raw byte, child terminal receipt, journal fsync, SQL CAS, prepare intent, keyed bus append, and cursor transition. Recover only an identical already-durable receipt/intent; ambiguous state remains UNKNOWN.
4. Cross-object and cross-process same-ID claimant and competing ordinary prompt cannot both send or settle. Prove lock order outside wire→bus→registry→SQL→journal and owner-controlled exact-once turn lifecycle.
5. On a separately authorized disposable root after exact-head independent review only: ordinary prompt → selected turn → one keyed response → reconnect → second ordinary prompt in **the same child**; no fabricated death. Provider/live is not authorized by this document.

**Open gates:** authoritative same-child terminal/pre-tool/capability source patch and review, no-follow fd-based first-source session enrollment, PR95/PR112 journal reconciliation, PR116-owned complete migration, exact owner/claim/turn bridge and independent integrated security review. Until then selected ACP coding wake stays OFF and no-STARTED; PR95 remains UNKNOWN.
