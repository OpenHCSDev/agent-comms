# Independent-review corrections: journal durability and orphan deadline

Scope: corrections on top of `2b9e7f407822720081e13ac20ed77b7ed227e41d`.
These do not activate adaptive compaction or certify remaining native writer
coverage/deployment. No installed package or live user session was changed.

## Per-transaction durable intent

The independent DELETE/FULL finding was valid: a constructor directory fsync
cannot cover a later rollback-journal unlink. `CompactionJournal._transaction`
now requires **effective** DELETE + EXTRA (`PRAGMA synchronous == 3`) and performs
an explicit parent-directory fsync **after every COMMIT, before returning**.
Any COMMIT/post-COMMIT sync exception raises `CompactionJournalUnknownError`;
`begin` cannot return an accepted intent and no native dispatch follows it.
An outcome sync exception never authorizes replay or an assumption of no-write.

Tests include verified effective settings, refusal when a setting is ineffective,
per-transaction directory sync, a real Python `os.fsync` fault after constructor,
and a native-bridge no-dispatch control with a retained unresolved intent.

Real `/var/tmp` strace of the independent order probe against corrected source:
`/var/tmp/pr48-journal-corrected.strace`, lines 347–365:

- after constructor marker;
- journal and database fdatasync;
- rollback-journal unlink at line 357;
- directory fdatasync at 359 (SQLite EXTRA);
- explicit directory fsync at 362;
- only then the after-begin marker at 365.

## Deadline survives Python owner death

Inherited lock FDs established safety, **not a deadline**. The transport now
launches a gated native child, opens a Linux pidfd for that exact process, and
arms a separate watchdog before releasing the exec gate or writing the request.

- Parent death before arming closes the gate: launcher exits without native exec.
- After arming, the watchdog independently enforces an absolute monotonic
  deadline, including when native JS is synchronously hung and the owner dies.
- Watchdog receives only the pidfd, never registry/wire/bus/input/executor FDs.
- It signals through pidfd, never a recycled PID; process exec preserves the
  canonical owner-parent lineage used by the native helper.
- Acknowledgement happens only after successful pidfd readiness registration.
- Live-parent cancellation still kills/reaps both children before normal return.
- Unsupported platforms/kernels fail closed before creating an intent in the
  bridge. This bridge is now **Linux-only**, not generic POSIX. Linux Python
  builds missing the Python pidfd wrappers use libc's named pidfd functions;
  there are no architecture-specific syscall numbers or PID-based fallbacks.

Kernel uninterruptible I/O can delay process exit after SIGKILL; no userspace
watchdog can override that kernel condition. Native locks remain fail closed
until exit; stale per-entry locks still require explicit operator recovery.

## Executable controls

The real-native SIGKILL test gates the actual CAS after stdin/authority parsing.
Its hung variant never releases the synchronous native read. After owner kill,
all five authority exclusions persist until the surviving watchdog kills native
at the configured three-second deadline. A registry waiter then completes,
session bytes remain unchanged, and fresh-owner exact-ID reconciliation proves
no-write. The released variant still commits once and reconciles without replay.
There are no production native fault hooks.

Additional tests prove failed watchdog arming cannot exec native, and parent
SIGKILL during watchdog setup releases the gated child without any mutation.

Focused provider-free suite: **186 passed, 1 skipped**;
`/var/tmp/pr48-durability-watchdog-tests.log`. Ruff/Black/mypy checks pass.

## Remaining gates

Independent review by `pr1-goal-p15-sink-independent-review` is required. Native
all-writer audit has separately found unfenced loader newline repair/fork writes
and a post-load revision-refresh race; those must be corrected before deployment.
Full package provenance, idle persistent manager/runtime integration, keyed
metadata-only publication coupling, and end-to-end adaptive operation remain
unfinished. No summary broadcast or new bus recipient is introduced here.
