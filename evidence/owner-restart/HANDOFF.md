# Owner release before process exit — restart correction

Owner: Darwin. Based on main PR186 (`27aee9a`). Branch `fix/owner-graceful-restart-20260928`; persistent tree `/home/ts/wt/comms-owner-graceful-restart-20260928`. Parent owns integration and live activation. No live state, processes or provider calls changed by this task.

## Actual cause and replacement

`release()` persists STOPPED and advances admission before the Python process has necessarily exited. Normal restart's three-second grace timeout entered `_require_same_stop_owner`, which required the old active status/admission generation. The final post-exit path already understood exact release receipts; the escalation path did not. This rejected the original owner as a replacement and aborted the batch before launch. Parent activation and recovery receipts independently show both old owners stopped, then both explicitly started without bus sequence change (56).

The existing lifecycle owner now accepts its existing exact voluntary-release receipt at that escalation boundary. This binds PID, name/creation identity, admission before/after and full released thread declaration. It does **not** substitute for process exit or local process identity. Before any KILL the original process must still have fresh participant proof. A replacement PID, new generation, altered receipt or unverifiable live process still fails. A zero-duration OS exit observation handles exit between grace expiration and escalation, including disappearance during local proof. Bulk restart still validates all escalation targets before signaling them and waits for OS exit before any replacement launch.

The same race applies to `stop()` and guarded restart. Guarded restart validates a voluntary release against its own persisted stop generation, preserving the pre-signal idle admission fence. No input, claim, UNKNOWN, queue or recovery policy changed; no new receipt/store/identity authority or compatibility API.

Changed production file: `src/agent_comms/owner_lifecycle.py`. Tests: new `tests/test_owner_release_restart.py`; two-line existing `test_operations.py` fixture adjustment so its post-KILL mutation only runs on the post-KILL wait, not a new nonblocking exit observation.

## Evidence and exact limits

Interpreter: `/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python`; `-m pytest -o addopts=''` disables configured xdist/coverage defaults. Commands bounded by `timeout 60` (baseline reproducer 15). Set `PYTHONPATH=/home/ts/wt/comms-owner-graceful-restart-20260928/src`; subprocess fixtures also explicitly set this absolute source path.

- `process-regressions.txt`: exit 0, **17 passed in 18.52s**. Actual isolated Linux subprocess handles TERM, calls real owner release and remains alive. Normal/guarded restart and stop escalate after the real three-second grace, and replacement callback asserts OS exit. Natural exit after grace causes no KILL. Replacement PID/generation, damaged receipt and lost participant proof refuse escalation/launch. Launch callback in these race fixtures is local; existing lifecycle test below covers a real replacement owner.
- `baseline-regression.txt`: deliberate exit 1, same new real-process normal restart case on parent main186 source: **1 failed in 3.28s** with the incident's `Owner changed while stopping 'worker'; refusing a stale signal`. No live owner was used.
- `existing-lifecycle-verified.txt`: exit 0, **108 passed in 6.69s** for `tests/test_restart.py tests/test_operations.py tests/test_prompt_restart_queue.py`, basetemp `/home/ts/wt/.ors28/verified`. Includes real idle owner replacement/session preservation, bulk preflight, PID/epoch/ABA rejection, process proof and queued restart admission.
- `git diff --check` passes. New test file formatted with Black.

Earlier attempts retained, not counted as passes: initial interpreter lacked metaclass_registry; subsequent full lifecycle attempt used relative PYTHONPATH/long fixture paths, causing detached-child startup failure and timed out (124). The absolute-path attempt then exposed an existing mock performing post-KILL release inside the newly introduced zero-duration probe (wire-lock recursion); it timed out (124). Corrected the fixture's phase, retained its ABA assertions, and the complete batch passed. `restart-first.txt` isolates the initial detached-child fixture failure. These are not reported as green suites.

## Parent action

Merge this branch on186/current main, build/install through the existing serial activation procedure. Use normal `owners.restart_owners` for the intended idle owners; no forced replay, registry edits or manufactured release/exit evidence. Parent's earlier explicit `owners.start` recovery remains valid and is not repeated here. No CI or additional provider acceptance gate is requested. Real-process evidence here is Linux; no new macOS execution claim. An unverifiable process that remains alive must still fail rather than receive an unsafe signal.
