# P107 retained Goal conversion (unfinished)

Integration owner: core-goal-retry. Public execution/publication: Codex only.

## Existing facts and owners

- `goals.py`: pre-P107 progress strings are already accepted by GoalProgress;
  preserve the original string, report, revision, state and reporting turn.
- `GoalMentionSource`: pre-P107 bound sources have no author. The first carrier
  adds the ordinary-author case only for that authenticated old declaration.
  Missing mention sources stay missing; no bindings or owner proof are invented.
- `GoalHistoryEntry`: convert only `before` and `after`. Preserve sequence,
  owner, timestamps, kind and all states, including pending/uncertain.
- `OwnerReleaseReceipt.thread`: retained stopped records contain Goals too.
- `RegistryStore._write_unlocked`: owns registry publication and its private
  guard. Raw replacement would break the existing guard protocol.
- `OwnerRestartHandoff` / `StoppedOwnerBatch`: already own original stopped
  identities, launch resources and the inherited wire descriptor. Extend this
  path; do not implement another fence/stop/launch algorithm.
- Old Todo rows do not contain the new required acceptance/source-location facts
  or goal-owned assignment. Refuse existing stores rather than invent these.
  The inspected public root has no Todo store.

## Checked checkpoint

`tools/cutover/native_schema_carry.py --goal-format-candidate ...` authenticates
copied originals with the actual installed source codec, creates separate
candidate files, converts all three Goal-containing stores, and checks them
with the target codec. It never publishes or launches anything.

Proof: `.artifacts/p107-format-journey.py` and
`.artifacts/p107-format-journey.log`; installed target Core c8c4eb53f, source
ff5bcd2aa. Frozen copies under
`/home/ts/.cache/agent-scratch/gr107/format-proof-1` preserve all 37 current
Goals, all 529 observed history rows (the earlier inventory had 528), and all
retained release receipts. Each store was copied independently, not as an atomic
public snapshot. No provider turn, input, stop or public write occurred.

## Remaining work before bringing P107 back

1. Revert the P107 revert on current main in this same PR once that revert is
   published. Keep the accepted feature version, not a competing implementation.
2. Finish the stopped original-decoder to target-decoder transfer using
   `OwnerRestartHandoff`, the original inherited wire descriptor and the existing
   cutover child. Target-only conversion must happen before target registry reads.
3. Integrate format preimages into RuntimeInstallation and the publisher's
   protected-file partitions. Verify Goal history semantically against originals;
   preserve all unrelated input/native/UNKNOWN files byte-for-byte.
4. Convert release receipts produced by the original stopping phase, not merely
   a pre-stop capture. Publish registry through its existing guard writer.
5. Verify all Goals, history rows, release receipts, private guard, retained
   settings and exact target launch on a copied store. Missing mention-source
   Goals currently still lack usable author behavior and need an owner-level
   decision; decoding alone is not usable acceptance.
6. Codex supplies a new reviewed pair and executes the complete retained
   publication. Verify the real saved-session UI afterwards. Do not publish the
   current candidate as a partial conversion.
