# Final S13 Thread cutover contract

Declaration authority: src/agent_comms/threads.py and child_process.py on
PR232, source implementation at62adf9a (receipt/handoff head a4c0dc0).

## Current stored fields

- `Thread.process_identity: ProcessIdentity | None = None`.
- `ProcessIdentity` has exactly `pid: int` and `start_time: int`, both positive.
  Start time is the opaque native platform birth token, not a wall-clock time.
- A2/FieldCodec JSON field is `process_identity`: null when unbound, otherwise
  an object with `pid` and `start_time`. Thread has no stored `pid` field.
- `Thread.pid` is a derived Python projection (0 when unbound).
  `Thread.process_alive` compares the saved birth against the OS.
- `_generated_created_at` is init=False and is never persisted by FieldCodec.

## One-shot conversion after quiescence; no live mutation here

For every retained Thread record, remove the former `pid` key and set
`process_identity` to null and `active_turn` to null. Clear any pre-cutover
process_identity as well: do not attest an old owner by looking up its current
numeric PID. Relaunch is the only source of a fresh binding.

Preserve exactly:

- Thread name and stored created_at (no clock/default/session timestamp synthesis).
- Tags, worktree/provenance, parent/task/title, session_file, model/thinking_level,
  goal and the remaining declared metadata.
- Thread channel_scope_generation, turn_generation, last_finished_turn_id and
  last_goal_report_turn. These are historical/fencing witnesses; clearing a live
  turn neither resets counters nor invents a completed turn/report.
- Registry aliases, statuses/last_seen and both GenerationCounter documents:
  owners.counter/owners.generations and admissions.counter/admissions.generations.
  Preserve their tombstoned generation entries too. Any separate parent-selected
  quiescent status transition is distinct from this format conversion.
- All durable goal histories and their original identities/order/revisions.

Construct/encode the converted current document directly; do not replay
register/unregister/goal operations to rebuild it (they advance counters or
produce new history). Validate with RegistryDocument.from_wire after integration.
Fresh launches subsequently advance owner/admission generations through normal
registration. No converter or old-format reader belongs in src/.

## Runtime release receipts

Reset `owner_release_receipts.json` to `{}` at the same quiescent cutover; old
receipts must not attest newly launched owners. New records are A2
OwnerReleaseReceipt(before:int, after:int, thread:Thread), keyed by thread name.
Their thread must have active_turn=None and after>before>0. Do not migrate stale
PID or serialized-Thread receipts into this authority store.

Parent owns one-shot converter, installation, reset and quiet relaunch.
No saved/live stores have been modified by S13 for this contract.
