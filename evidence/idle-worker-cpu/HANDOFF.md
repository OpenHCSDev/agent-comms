# Idle worker CPU — source ready for parent installation

Base: main fdfab5f (PR220). Branch: fix/idle-worker-cpu-20260928.
Parent owns installation, owner restart, live CPU comparison, provider wake and pins.
No production mutation, signal, stop or provider call performed here.

## Verified cause

- Original idle owners measured 60%/62% over a fresh 3s interval, same PIDs,
  active_turn=None before/after (`live-idle-cpu.json`). Parent also measured 74%/73%.
- Read-only elevated py-spy `--nonblocking` captures show repeated native package
  tree hashing, historical cohort reacceptance/full verified bus scans and cursor
  coverage. Raw samples preserved; nonblocking capture had errors (UX89 samples,
  126 errors; PR9578 samples,130 errors), not exhaustive profiles.
- Actual six-second directory event sample found zero watched authority events.
  SQLite/lock-file closes are excluded by the existing watcher. No evidence that
  SQLite self-writes caused watcher busyspin; periodic scans caused redundant work.
- CoordinationStore unconditionally chmodded existing files on open AND close,
  altering ctime on observations. This prevented quiescence revision settlement.
  It now repairs modes only when different; existing privacy enforcement remains.

## Complete changed ownership/callers

- `input_drain.py`: ephemeral per-session quiescent observation, derived from
  existing file revisions and current session controls. A concurrent change
  prevents memoization; a completed native turn is never memoized. No longer
  interval or disabled watcher. Normal config/goals loop remains in place.
  Invalidations: canonical bus; registry + private guard; coordinator DB
  (DELETE journal, including recovery/claim rebuild); root identity; package path;
  wake/runtime controls; binding; backend inbox and active turn/task membership.
  Private marker is validated before every skip. No receipt/delivery authority
  is cached.
- `coordination_cohort.py`: sealed sequence projection from immutable existing
  receipts; shared bounded `next_sealed_assignment` owns selection previously
  inline in SelectedExecution. No second selection store/roster.
- `cohort_foreground.py`: reaccept only unsealed originals; no repeated historical
  full-N transactions. New ACP cohorts still preflight package before SQL acceptance.
- `acp.py`: only enter SelectedExecution when receipt-backed work exists and the
  participant execution slot is free. Cursor/source checks remain on cold reads.
- `coordinated_runtime.py`: consumes same selection owner; `_open` package
  verification and original-source validation on actual sends remain intact.
- `coordination.py`: avoid no-op chmod ctime invalidation, retain 0600 repair.
- Tests: `test_private_idle_drain.py`, `test_wire_watch.py`.

## Acceptance

`PYTHONPATH=src TMPDIR=$PWD/.artifacts/tmp timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -o addopts='' -n0 tests/test_private_idle_drain.py tests/test_wire_watch.py tests/test_acp_private_nk_delivery.py -q`

**37 passed in 10.42s**, `focused-final.log`.
Covers idle skip; mode/revision preservation; permission repair; registry changes;
config toggles; new inputs; coordinator-only recovery revision; change during await;
turn completion; blocked overlap; package/root admission guards; normal selected
processing and watch filtering. Earlier failed receipts retained in focused.log.

Copied actual live root at sequence85, read-only file snapshots and read-only SQLite
backup, with source revisions checked unchanged during copy. Both actual owners
rebound only in disposable copy; copied inode certificate regenerated through the
existing installer, canonical bytes unchanged. No provider/package invocation
allowed. Actual claims/native inputs/execution pointers preserved exactly.

- UX settles after 2 cold scans, 20 warm polls => **0 scans**.
- PR95 settles after 1 cold scan, 20 warm polls => **0 scans**.
- Coordinator revision stable; canonical bus unchanged; claims/receipts/native
  inputs/execution pointers unchanged. Copy deleted on successful exit.
- `copied_live_idle.py` / `copied-live-idle.log` retain reproducible method/evidence.

## Remaining boundary

No source blocker. Parent must measure installed idle CPU and confirm a fresh real
channel wake with configured provider. Source/copy evidence is not live performance
acceptance. Historical UNKNOWN remains unchanged; no retry/replay introduced.
