# Native post-write admission failure

Implementation tree: `~/wt/comms-native-postwrite-cause-20260928`.
Branch: `fix/native-postwrite-cause-20260928`.
Developed on installed integration e211e50; the single fix commit was rebased
cleanly onto current fork main 0ac0568, excluding inherited integration history.

## Read-only live evidence

Input `5c94f104375621015cc0c22ba5848a0e` is UX triage of source76. Notice81
reported `Native prompt post-write outcome is UNKNOWN; no retry`. Coordinator
input has no sent admission epoch or session proof; assignment is deferred.
The private raw journal retains its UNKNOWN reservation. The corresponding
native file `2026-09-28T13-13-10-449Z_01a0e825-dc31-703e-9d17-4596d12b0390.jsonl`
contains user input entry120d28bb and its input-proof journal contains
context_committed generation1. No assistant terminal is present. These saved
records corroborate bytes reaching Pi; they do not grant recovered acceptance.

Worker3814550 has stdout/stderr on /dev/null and no AGENT_COMMS_DEBUG_LOG. Its
diagnostic contains no exception cause. The wrapper used `raise ... from error`
but omitted the cause from its message, so the notice discarded that detail.
**The exact historical exception cannot be established.** Reader contention at
COMMIT is a locally reproduced mechanism consistent with the missing admission
commit and surviving raw/session evidence; it is not claimed as a proven live cause.

## Source fix

- `coordination_store.py`: existing store now owns `irreversible_admission()`,
  acquiring BEGIN EXCLUSIVE before external effects. Ordinary transactions retain
  BEGIN IMMEDIATE; both reuse the same non-nesting/commit/rollback implementation.
- `coordinated_runtime.py`: only native send admission uses this exclusive scope.
  The existing zero-timeout exclusion failure becomes PromptAdmissionBusy before
  reservation/bytes; the bounded writer may wait safely without retaining partial
  locks. Once admitted, readers cannot create a COMMIT lock-upgrade failure.
- `native_prompt_send.py`: post-write UNKNOWN includes underlying exception type
  and message and retains its cause chain. No post-write or COMMIT retry exists.

No WAL migration, wider timeout, parallel store, live changes, provider calls or
replays. Old UNKNOWN inputs remain UNKNOWN. Parent owns installation/restarts.

## Acceptance

`focused.log`: **62 passed in 12.62s**.

```sh
PYTHONPATH=src TMPDIR=$PWD/.artifacts/tmp timeout 60 \
 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest \
 -o addopts='' -n 0 tests/test_native_send_admission.py tests/test_coordination_store.py -q
```

New actual SQLite + nonblocking pipe case demonstrates the old failure:
BEGIN IMMEDIATE permits a held reader, the pipe receives one prompt, COMMIT
raises SQLITE_BUSY, admission rolls back and the wrapper preserves
`OperationalError: database is locked`. No second write or admission occurs.

New TRIAGE and FULL tests use actual SelectedExecution/native RPC pipe admission
and a local subprocess (no provider). A real reader held before admission refuses
the first probe before any prompt; after it releases, a late reader receives
SQLITE_BUSY while the exclusive send scope is held. Exactly one prompt is sent
and its admission epoch commits. The fixture child then exits without native
proof; that expected downstream failure is not a post-write admission failure.
Existing deadline/cancellation, no-reentry, owner-fence and store tests pass.

Changed test: `tests/test_native_send_admission.py`. No feedback/UI changes.
