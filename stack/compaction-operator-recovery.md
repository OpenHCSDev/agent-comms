# PR95 native compaction operator recovery (not an activation procedure)

**Status:** this describes the reviewed journal/writer and the still-dormant
owner sequencing path. There is no deployed adaptive trigger or canonical
manual owner bridge. Do not run these steps on installed Pi or a live session
without separate operator authorization, and do not infer completion from an
ACP notification. This document does not grant a new commit or retry.

## Stop and preserve evidence

1. Identify the exact canonical wire root, thread name, owner PID/createdAt,
   epoch, goal ID/revision, active turn, canonical saved-session file and
   persisted commit ID. Preserve the original JSONL, journal database,
   registry, input dispositions and private failure logs byte-for-byte before
   any repair. Never print a summary, prompt, auth material or full intent to
   a public bus/channel.
2. Quiesce the owner through the existing verified stop/lifetime mechanism.
   A process death or cancellation is **not** evidence of no native write:
   inherited writer and registry authority can remain in the bounded native
   child until its pidfd-enforced deadline. Do not steal/stale-break locks or
   send another prompt while that child may still hold authority.
3. Determine whether the durable `compaction-commits.sqlite3` operation is
   `intent`, `unknown`, `committed`, `refused`, or `aborted-no-write`. Read it
   from a forensic copy with SQLite `mode=ro`; constructing
   `CompactionJournal` on the original is **not read-only** (it verifies SQLite
   modes and syncs directories). Correlate by exact 32-hex commit ID and
   canonical session file, not by summary text or latest timestamp. An
   unreadable journal or untrusted session identity is a send barrier.

## Exact-ID outcomes

| Durable state | Operator action | What must never be inferred |
| --- | --- | --- |
| `intent` or `unknown` | Keep next input/commit blocked. After proving the prior native child exited and restoring a valid current owner claim, invoke only the trusted `OwnerCompactionCommit.reconcile` with the original exact commit ID under the native writer/registry fences. Preserve its result and re-check the journal and JSONL. | No automatic redispatch of the summary, no conversion to `refused` after UNKNOWN, no retry from timeout/cancellation. |
| `committed` | Verify native entry ID, session revision and leaf ID against the matching saved JSONL under the writer fence. The exact-ID pending outbox row contains only those metadata fields. Retire any stale idle RPC manager and require strict v3 disk plus fresh RPC session-ID/file validation before a **distinct** next input. | A returned ACP update is not a new summary or remote-display receipt. |
| `refused` or `aborted-no-write` | Preserve the terminal evidence. A later new compaction requires a new, freshly admitted owner source and new commit ID; it is not a replay of the old attempt. | An exception alone never proves this terminal state. |
| No readable operation after an uncertain begin | Preserve original bytes and seek explicit expert recovery. | Absence in one read does not establish that an earlier writer had no side effects or authorize reusing the ID. |

The trusted reconcile action is **observation only**: it never sends a summary
or a `commit` action. If native evidence is ambiguous, leave the row UNKNOWN
and inputs blocked. Registry owner replacement, changed goal/turn, changed
bus/input revision, changed session revision, or unresolved input disposition
invalidates a prepared summary; derive a new source only after resolving the
old uncertainty, never from a model-authored receipt.

## Local ACP metadata publication

The outbox is keyed by the same native commit ID. A row is enqueued atomically
only with an exact verified committed outcome; `intent`/`unknown` never
publish. An attached local owner ACP session can project only
`commitId`/`entryId`/`revision`/`leafId`, with no summary, recipient or bus
broadcast. A transport failure before mark leaves the row pending; a
post-COMMIT parent-fsync error during `observe_publication` may leave it
**pending or already observed**. Re-read the exact row; never assume rollback
or treat notification uncertainty as native commit authority. Reproject only
pending rows to the matching canonical owner/session under the local handoff
fence. UI consumers deduplicate by exact commit ID; local acceptance is not
proof of remote display.

## Fresh reopen and escalation

After any external native write attempt, discard the old idle manager **before**
writer mutation. An interrupted retirement preserves its reopen-required
marker; later borrowers must wait for the exact old child to be reaped. Strict
read-only pinned v3 JSONL validation and a fresh RPC `get_state` must agree on
session ID and canonical file before a distinct input may reach a provider.
Torn tail, missing file, alias, legacy header or changed ancestry is a hard
refusal: preserve it, do not truncate, rewrite, migrate or silently fall back
to separately installed Pi. If the exact owner cannot be safely re-established
or verified, keep the operation blocked and request human recovery; do not
invent a publication recipient or replay an UNKNOWN input.

Provider-free acceptance uses disposable pinned package tree
`628b68df3c1cc91e6b6698eb639ff108b4835d027cc34a219087861664d01a07`
and isolated wire/session roots. The package admission fence is scoped to its
audited sources and subprocess sinks; this is **not** a global OS sandbox.
Independent combined review, live main/PR verification, and explicit release
authorization remain prerequisites for any activation or merge.
