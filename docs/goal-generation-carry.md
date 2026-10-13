# Existing goal generation carry (#784)

Owner: doctrine-segment. Source branch: `fix/existing-goal-generations`.

## Authorization and prior uncertain operation

Coach source 3885 explicitly disposed the earlier interrupted Bash admission as
**abandoned**: do not rerun that command; continue with fresh distinct commands,
checking state before each write. Readback showed the original branch/tracked
source unchanged and no carry write had been issued. The abandoned command was
not retried. Coach then explicitly requested the backup-first one-time carry on
the live store, with no bus copy under lock. Shepherd's preview review is not
write approval and is not being used as such.

## Source owners and meaning

RegistryStore/FieldCodec own public goal decoding. Generation and AttemptRecord
own private reads. The temporary outside-src tool uses GoalAttemptStore's
existing create_goal, transaction, sync and readback behavior; it adds no runtime
repair path. The ledger backup runs outside all wire/registry locks. Each write
rechecks the exact current goal and owner incarnation, absence of a generation
and absence of recorded attempts under wire custody. Existing rows are never
replaced. Public goals, statuses, revisions, input history and attempts are not
written by the carry.

**Zero recorded attempts does not prove that a goal never ran.** The carried
READY row describes a newly recorded unreserved generation. It does not describe
past execution of the goal. BlockedState owns refusal of ordinary Active/Resume
transitions for every actor; only the existing explicit owner Retry action may
reactivate a blocked goal and authorize a fresh generation. UNKNOWN input remains
untouched and is not the fresh continuation's source.

Goals already owns store opening. OwnedTurn.stream now asks that owner to open
the store through Coordination.run_worker before creating OwnedSendAdmission.
The only constructor no longer reads the nullable cache. OwnedSendAdmission
carries a non-null store and deletes its redundant nullable assertion. The Goals
cache rename/lifecycle follow-up is deferred as requested.

Before-source census used the existing refactor-audit Package parser: 307
production modules, no omissions. Owner opening/cache writes and every production
consumer were traced; OwnedTurn.stream was the only external cache reader and
the sole OwnedSendAdmission constructor. No whole dependency/dynamic binding
proof is claimed.

## Live census and retained evidence

Initial read-only census: 17 goals missing a generation, all with zero recorded
attempts. Sixteen are archived/stopped; the sole running candidate is
codex-shepherd's blocked goal. Public blocked/paused/completed/archived state stays
unchanged. paper1-editor already has generation 2, BLOCKED, with two attempts; its
ongoing turn and recorded attempt are excluded from the carry.

Owned operator evidence is under
`/home/ts/wt/core-doctrine-segment/.scratch/` (preview and source census receipts).
Prior accepted physical #777 capture/receipt:
`goal-physical-20261013-final/physical-final-20261013-191/`.
That check exercised new goal creation; it is not historical-carry acceptance.
Failed native/session/input originals remain preserved.

Live carry count, backup receipt and affected installed acceptance follow here
only after the actual operations finish. The temporary tool will be deleted
before the PR is marked ready.
