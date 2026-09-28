# R4 goal-attempt lifecycle closure

Pascal owns source in `/home/ts/wt/comms-refactor-r4-goal-attempts-20260928`,
branch `codex/refactor-r4-goal-attempts-20260928`, initially main186 `27aee9a`.
Parent owns integration, paired Toad and activation. R1–R3 are outside this change.
Current checkpoint: implementation complete and local acceptance passed on186;
reconciling main188 restart fix before publication and repeating the affected
local process seams as explicitly requested. No observed production failure.

## Real ownership and removed mechanisms

- Existing GenerationState owns identity shape, retry/resume/retirement behavior
  and transitions. GoalAttemptPhase is a distinct DeclaredFamily/LifecycleState
  for reserved/claimed/failed/succeeded/resolved attempts. The distinction is
  necessary: a RESERVED generation contains either a reserved or claimed attempt.
- GoalAttemptStore decodes each lifecycle row through FieldCodec into Generation
  or AttemptRecord; transaction consumers use typed values. Malformed stored
  state is StorageUncertainError, never a grant or inferred repair.
- All generation/attempt UPDATE transitions use the shared store mutation paths,
  checked against declaration-owned successors. Retirement's parallel string
  switch and phase sets are deleted. DDL choices reuse existing sql_names;
  write/query spellings derive from declarations, including failure projections.
- Deleted Generation's string constructor and `.state` property. Current internal
  construction is `Generation(id, number, ReadyGeneration(), None)` etc.
- Deleted StorageUncertain, ReservationConflict, UnresolvedAttempt and StaleAttempt
  aliases. Their actual `*Error` declarations are the sole API; all current core
  and test imports/catches migrated. OwnedTurn uses ClaimedAttempt and
  ReservedGeneration directly; TurnRunner/TurnProgress only change error names.
- No replacement database, capability store, provider/usage protocol or replay
  mechanism. Process-local grants and reservation ownership, BEGIN IMMEDIATE,
  commit→fsync→fresh readback, one-use claim revocation and human decision uniqueness
  remain intact. Persisted schema5 keeps its existing names; real version2–4 data
  migration remains. This is saved-data preservation, not old-client support.

## Local acceptance on186 (before requested188 reconciliation)

Existing integration venv; `PYTHONPATH=$PWD/src`, `pytest -o addopts=''`, xdist off.

- store-tests.log:78pass,1Windows-only skip. Includes competing real processes,
  crash after reservation/claim/ready commit, uncertain fsync, no replay, grant
  secrecy, terminal states and actual saved-schema migration.
- consumers.log:171pass,2 stale test assertion failures (`Generation.state`).
  Repaired both current callers; original failure evidence preserved.
- boundary-and-repair.log:7pass (the two repairs +five boundary/history cases).
  Unknown/malformed state never authorizes a launch; malformed attempt rolls back
  without consuming a decision; saved UNKNOWN/failure evidence survives explicit
  retry/retirement.
- native-seams.log:5pass in18.30s, using prepared installed pi-native against
  network-fenced localhost HTTP fixtures: retry waits for unrelated response,
  cancel/retry needs a new grant, standby/resume. No paid/provider network calls.
- Total unique acceptance:261pass,1skip. Focused/local/native fixture evidence;
  does not replace parent's actual configured-provider or installed UI evidence.
- NRA baseline:full context,79detectors,complete,0findings. That baseline also had
  the concrete R4 debt, so a zero count is explicitly not closure evidence.
- Exact NRA commands are retained in nra-before-command.sh/nra-after-command.sh.
  The latter includes the new phase module; artifacts include scan_status.

## Parent / Toad

Read-only search of current installed Toad source found no goal_attempts imports,
Generation construction or removed exception alias references. No production
Toad API patch identified. Tests outside core should construct/use nominal
`.lifecycle` values if they inspect store snapshots. No other worker files edited.
Owner-current restart188 reconciliation and process results will be appended here.
