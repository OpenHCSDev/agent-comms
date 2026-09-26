# Compaction conflict inventory (production successor, not activation approval)

## Registry authority

The complete `ThreadRegistry._save_unlocked()` call inventory at this checkpoint:

| Writer | Serial boundary | Relevant invalidation |
| --- | --- | --- |
| `register` | registry `_store_lock` | goal/status/metadata/epoch replacement; admission when ownership changes |
| `_claim_turn_unlocked` | callers `claim_local_turn`, `claim_live_turn_with_epoch`, `claim_live_turn_with_admission` hold registry lock | active turn and epoch |
| `finish_claimed_turn_with_fence` | registry lock | active turn release and completed-turn fence |
| `rename` | registry lock | canonical identity, aliases, turn/owner metadata |
| `unregister` | registry lock | stopped status and admission/epoch invalidation |
| `archive` | registry lock | presentation/lifecycle state and admission/epoch |
| `begin_delete` / `remove` | registry lock | deletion and ownership removal |
| `heartbeat` | registry lock | last-seen; revival changes admission/epoch |

`Comms.update_goal` ultimately uses registry `register`; it does not own a
separate unguarded registry write. The retained guard reloads and checks full
expected Thread equality plus active status, actual PID, executable role,
claimed turn ID/epoch, and active exact goal ID/revision.

Source-audited direct registry lock boundaries outside the class are
`operations.py` (`mark_dm_view_read`, an identity read held through marker CAS),
`claim_admission.py`, `coordination_response.py`, and private-protocol
initialization in `declarations.py`. This table does not claim those surrounding
multi-store transactions are atomically coupled to compaction merely because
they share a registry lock.

## Other authorities and lock order

There are **two distinct session locks**:

1. `session_fence.session_writer_fence` is a Python executor-lifetime lock.
   `backend.stream_agent_events` holds it BEFORE calling ACP send boundaries.
   Those boundaries acquire wire/registry locks. The external compaction helper
   must therefore acquire the same executor slot FIRST, nonblocking; taking it
   while holding registry authority can deadlock an active backend.
2. The patched native `.pr48-writer.lock` is the per-entry file mutation fence.
   It comes AFTER registry authority in the compaction path.

Current external bridge order: executor slot → registry → native-entry lock.
Both executor and registry descriptors are inherited until native child exit.
An idle persistent Pi process is not ruled out by the executor slot; runtime
integration must explicitly close/reopen it around an external helper commit.

Required **future ingress/publication boundary** must also respect existing
wire → bus → registry order. Evidence:

- `Comms.send_message` takes wire, then `MessageBus.publish` takes bus and calls
  `_prepare_message_unlocked`, which obtains a registry snapshot.
- Private claim publication and keyed response paths similarly use bus then
  registry, with explicit already-locked snapshot variants.
- ACP `send_boundary` takes wire through final input validation and stdin write;
  `InputDispositions.record`/`bind`/`started` have their own ledger lock.
- Input receipt rows are UNKNOWN until exact native user-start evidence. They
  are not replay requests, and their ledger is not owned by the registry lock.

Therefore **never acquire bus or executor locks from inside the existing
registry guard**. A future combined boundary needs executor → wire → bus →
registry → input ledger → native entry, after proving no reverse edge in every
caller. Every necessary outer exclusion must also survive parent death through
retained descriptors. This order is a design target, not yet implemented or
independently reviewed.

## Open gates

- Capture an ingress witness before summary preparation, compare it under the
  final boundary, refuse newly admitted/queued corrections and in-flight inputs.
  No caller-stamped correction counter may substitute for canonical evidence.
- Retain wire/bus/input exclusion through mutation, and couple result publication
  through a durable keyed outcome/outbox rather than replaying native mutation.
- Verify all native writer paths and transitive package provenance during
  deployment; the dormant per-entry patch is not yet applied by preparation.
- Extend deterministic race tests to raw bus sends, ACP queued/steered inputs,
  every lifecycle writer, and idle persistent-session invalidation.

No live activation is safe before these gates and independent review.
