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
transitions for every actor; only the explicit Retry action, taken by the unit,
its commander or the owner, may reactivate a blocked goal and authorize a fresh
generation. UNKNOWN input remains
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

Live carry completed: **17 recorded; zero remaining missing rows**. The source
checks before each write passed. paper1-editor's recorded generation 2/attempt
were excluded; codex-shepherd's public goal stayed blocked. The backup is
`.scratch/existing-generation-live-backup-20261013/goal_attempts.sqlite3`, with
per-write intent/readback and before/after census in adjacent `receipt.json`.
The SQLite backup was made outside wire/registry locks; no bus was copied.

Offline carry proof: three missing rows recorded, repeat zero; original public
registry and UNKNOWN input bytes identical, existing attempted generation
unchanged; Model/Runtime/Owner ordinary Resume refused; explicit Owner Retry
created generation 2 for the same goal without replaying UNKNOWN. Focused serial
batch: 34 passed. The first default pytest invocation had 33 passing tests but
failed the whole-project coverage gate; this is not a full-suite pass.

The temporary tool is deleted after the live run (retained in branch history).
The opened-store consumption change still needs merge/installation.

Candidate clean-export wheel e21a2615 was installed in an isolated environment,
reusing current Toad #606 dependencies without changing the live pointer. The
real Toad App/Pilot and ACP/native process opened SDK-authored saved history with
a pre-generation blocked goal. The actual Retry widget first refused missing
launch authority, with zero provider calls; creating the fresh carry row left
the same public goal blocked. A second actual Retry click completed the same
goal at generation 2: two controlled localhost requests, two saved user inputs,
original history byte prefix preserved, native child retired. This is installed
headless UI acceptance, not a new physical st or paid-provider claim. Receipt
and rendered SVGs: `.scratch/existing-generation-installed-journey-v2/`.

The initial UI check timed out searching for an instruction beyond the clipped
single-line error flash; it made zero provider calls. That original is retained
in `.scratch/existing-generation-installed-journey/`. The successful check used
a fresh session/root and asserted the visible `no launch authority` text.

After-source census: 307 production and 359 test modules, no parse omissions.
The sole admission constructor is OwnedTurn.stream and now calls the opening
owner. All nullable cache reads/writes are inside Goals; admission's distinct
non-null field is its supplied capability. Evidence:
`.scratch/existing-generation-owner-{before,after}.json`. Python AST does not
prove compiled JS, dependency dynamic lookup or runtime equivalence; native/UI
acceptance above is the behavioral check.
