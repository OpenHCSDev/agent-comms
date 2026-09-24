# Claims on the envelope

A design for making resource ownership a property of sending, so that late message delivery can no longer produce duplicate owners.

Written to be handed to an implementing agent. A default-off, cooperative **ADVISORY** primitive may be bootstrapped without closing every Section 8 question. An integrated envelope claim+announcement must still solve crash-atomicity before claiming durable authority. Scope is owner-set in `plans/trust-boundary.md`.

---

## 1. The failure

Two coordinators assigned the same backend file before seeing each other's messages. Neither was wrong about what it saw. Ownership was being derived from message arrival order, and arrival order is an observation, not an authority. Two observers legitimately disagree.

Ordering discipline cannot fix this. The agent that acted first had a correct view at the time it acted.

## 2. The primitive

`O_CREAT|O_EXCL` file creation is atomic on POSIX. Exactly one caller succeeds; every other caller gets `EEXIST` and can then read the file to learn the winner.

```python
fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
```

For replacing existing state, `os.rename()` within one filesystem is the atomic counterpart. No torn reads, no partial states.

This is not locking. A lock is held across work and fails on hold time, liveness, and timeout tuning. A claim is a durable record of who owns what, with a sequence number. Nobody waits on a claim, so nobody can be starved by one, and there is no timeout to tune. An agent that loses a claim does not block; it does not edit, and it says so.

## 3. Why it goes on the envelope

The agent already announces ownership when it sends a handoff. Requiring it to *also* call `claim()` is the manual-enumeration pattern: a fact that a containing thing already determines, maintained separately. Two records that must agree, with nothing making them agree.

So the claim is an envelope field, and the wire arbitrates as part of the send.

```
send(to=..., type="handoff", claims=["backend/x.py"], body=...)
send(to=..., type="ack",     releases=["backend/x.py"], body=...)
```

One call. The one the agent was already making.

One property follows at the API boundary: a conflict can be reported **synchronously to the deciding agent**, rather than discovered later from messages. **One API call does not make the claim file and wire append crash-atomic.** A crash between them can diverge ownership and announcement; this remains the blocking durability design question for integrated envelope sends.

## 4. Send path

On `send` with a non-empty `claims`:

1. Normalize each resource id (see Q1).
2. Attempt `O_CREAT|O_EXCL` on `<claims_dir>/<encoded_resource_id>` for each.
3. On full success: write the claim record, append the message to the wire, return success with the claim ids.
4. On any failure: perform the partial-acquisition policy (Q5), do **not** append the message, and return a failure carrying the current owner, their sequence number, and the claim timestamp.

On `send` with a non-empty `releases`: verify the caller owns each claim (Q10), unlink, then append. Release is atomic with the message reporting the work done, so there is no window where the work is announced complete but the resource is still held.

### Claim record

Written into the claim file at creation:

```json
{
  "resource": "<normalized id>",
  "owner": "<agent name>",
  "session": "<pi session id>",
  "seq": <wire sequence number>,
  "message_id": "<id of the claiming message>",
  "ts": "<ISO 8601>"
}
```

`session` matters for Q3. `seq` and `message_id` let any reader trace a claim back to the message that made it.

## 5. Delivery

Do not make agents query ownership. "Check current ownership before acting" is a discipline, and disciplines are what failed.

Delivery already injects messages into the agent's prompt. Inject the live claim table beside them. The cooperating agent sees a current ownership snapshot with the work and should decline a conflicting edit. This is **advisory**: a prompt snapshot may become stale before action, and direct `bash`/edit writes are not fenced. This design does not claim to make a stale-delivered write impossible.

Keep the table small: resource, owner, age. Full records are available on request.

## 6. Failure semantics

A losing send is a result, not an error condition to retry. The agent receives the current owner and decides: message the owner, pick different work, or stop. That decision is itself a message and is visible on the wire.

The wire must not retry on the agent's behalf. Retrying reintroduces a wait, and waits are what the design exists to avoid.

## 7. What not to build

- No timeouts on claims. See Q3 for the orphan question, but the answer is not a TTL.
- No queueing or waiting on a contended resource.
- No broker, daemon, or consensus protocol.
- No separate `claim()` / `release()` calls alongside the envelope fields. One path only.
- No read-locks unless Q7 says otherwise.

---

## 8. Open questions

These are mine, and they block implementation. Answer inline and hand back.

**Q1 — Resource namespace and normalization.**
What is a resource id? Repo-relative paths? Arbitrary opaque strings? If paths: relative to what root, and how are `backend/x.py`, `./backend/x.py`, `BACKEND/X.PY`, and symlinked paths reconciled? Two agents naming the same file differently must collide, or the whole thing fails silently.

**Q2 — Granularity and hierarchy.**
Is claiming `backend/` supposed to block `backend/x.py`? Hierarchical claims cannot be decided by a single `O_EXCL` syscall; they need a prefix scan, which is not atomic against a concurrent create. If hierarchy is needed, the design changes materially. If file-level only, say so and the primitive stands.

**Q3 — Orphans.**
An agent dies holding a claim. With no TTL, the resource is blocked forever. Options I can see:
(a) tie the claim to a pi session id and treat a session whose JSONL has stopped growing as dead;
(b) require an explicit supervisor release, recorded on the wire;
(c) a generation counter where a new claim can supersede an old one whose owner is provably gone.
Which, and what counts as proof that an owner is gone?

**Q4 — Worktree scope.**
Same path in two worktrees: one resource or two? If two, the resource id must carry the worktree, and then a shared file edited from two worktrees is *not* protected. If one, agents in separate worktrees block each other on files they could safely both edit.

**Q5 — Partial acquisition.**
An agent claims three resources and wins two. Options: (a) all-or-nothing with rollback by unlinking the won ones, which is not itself atomic and can race; (b) partial success, returning which were won and which were lost, and let the agent decide. I lean (b) because it introduces no rollback window, but it means an agent can hold a useless partial set. Your call.

**Q6 — Durability.**
Does the wire `fsync` the claim file before appending the message? Without it, a crash can lose a claim that a message already announced. With it, every claiming send pays a sync. Given the volume in the corpus, this is a real cost.

**Q7 — Read claims.**
Do you need shared-read / exclusive-write, or is exclusive-only enough? Exclusive-only is one syscall. Shared-read is a different and much larger design.

**Q8 — The losing message.**
When a send loses its claim, the message is not appended. Should the *attempt* be recorded on the wire anyway, as evidence that the agent tried? It costs an append and it makes the contention visible in the transcript, which your audit would then be able to measure.

**Q9 — Filesystem.**
Where does `<claims_dir>` live? `O_CREAT|O_EXCL` is unreliable on NFS. Your corpus mentions a worktree on tmpfs with git-dir on ext4. Claims must sit on a filesystem where `O_EXCL` is atomic, and `rename()` must be same-filesystem to be atomic.

**Q10 — Who may release.**
Owner only? Can a supervisor force-release, and if so, is that recorded as a distinct message type so the original owner can discover it happened?

---

## 9. Acceptance

The design is working when:

- two agents issuing conflicting claims concurrently produce exactly one winner and one synchronous loser;
- a cooperating agent receiving a late-delivered stale message sees the claim snapshot and voluntarily declines a conflicting edit; this is **ADVISORY (does not enforce: raw `bash`/edit writes)**, not an action-time write prohibition;
- no agent ever waits on a claim;
- the wire transcript is sufficient to reconstruct who owned what at any sequence number.

The last one is the audit property for an **integrated durable** envelope claim. The default-off advisory primitive can ship separately, but may not assert transcript-reconstructable ownership or claim+message crash-atomicity until those properties are implemented and checked.
