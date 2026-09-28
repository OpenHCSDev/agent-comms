# S13: Child-process supervision

**Head audited:** `agent-comms` `main` at `0c63715`; re-verify at yours. **Rules:** [00-RULES.md](00-RULES.md). **Builds** [A12 `ChildProcess`](02-SHARED-ABSTRACTIONS.md#a12-childprocess); **uses** A1, A2.
**Step 1:** build A12 as a new module. **Step 2:** migrate S13's files.

---

## What is wrong

Child processes are started, stopped and identified in eleven modules at four levels of rigour:

1. **Race-free, Linux only:** `selected_pi_child_deadline.py` runs its child as PID 1 in a PID namespace and kills it through a `pidfd`; `owner_compaction_process.py` arms an independent `pidfd` watchdog for a hard deadline.
2. **Process group, then graceful and forced stop:** `native_pi.py`, `manual_compaction.py`, `backend.py`'s pi child, `recovery_gateway.py`, each with its own grace constant (a `1.0` literal, `GRACE`, `_KILL_GRACE`).
3. **The direct child only:** `backend.py`'s two discovery children and five one-shot `subprocess.run(timeout=…)` calls, which leave anything the child spawned running.
4. **Bare PID, for the processes that most need identity:** `owner_lifecycle.py` launches long-lived detached owners, records only `pid=process.pid`, and later probes and signals them by that PID alone (`_process_alive`, `_signal_local_owner`, which uses `killpg` when the PID leads a group). After an owner dies and its PID is reused, a stop or restart signals an unrelated process, or its whole group.

---

## Target

**A12 in `child_process.py`**, lifting the tested implementations that already exist rather than writing new ones: the `pidfd` guardian and PID-namespace launch from `selected_pi_child_deadline.py`, the watchdog from `compaction_child_watchdog.py`, and the guardian's handshake with its namespaced child, whose records decode through A2.

- **One stop algorithm** on the base: graceful signal to the group, one grace period, forced kill of the group, reap. One grace constant for the whole codebase (2 seconds unless measurement says otherwise). `RPC_ABORT_GRACE_SECONDS` is a different role and stays separate.
- **Three shapes:** `BoundedRun` (one-shot, deadline, captured output), `AttachedChild` (long-lived, async, streamed), `DetachedProcess` (outlives its launcher).
- **Every child in its own process group, always.** Stronger containment (a PID namespace) is a capability a caller requires, provided on Linux and refused plainly elsewhere.
- **`ProcessIdentity`:** PID plus the process's start time, read per platform. Every liveness answer and every signal to a detached process checks it. **There is no code path that acts on a bare PID.**
- **A `Platform` family** with capabilities composed by multiple inheritance (`ProcessGroups`, `PidfdHandles`, `NamespaceContainment`), filtered with `members_with`, replacing every `os.name` and `sys.platform` branch about processes.
- **A `ChildOutcome` family:** exited with a code, killed by a signal, timed out at the graceful or the forced stage, failed to start.

---

## Migration

1. **Build A12** as a new module only. It collides with nothing.
2. **Migrate every spawn, stop and liveness call in S13's files:**
   - `backend.py`: the pi child becomes an `AttachedChild`; the discovery children become `BoundedRun`s and gain their own groups;
   - `owner_lifecycle.py`: owners become `DetachedProcess`es; launch records a `ProcessIdentity`; `_process_alive` and `_signal_local_owner` are **deleted**, not wrapped;
   - `recovery_gateway.py`: the snapshot process becomes a `BoundedRun`.
3. **Cutover:** owners launched before this change have no recorded identity. **They are relaunched at cutover.** There is no code for "old owner records."
4. **S9 and S10 adopt A12** in their own files and delete the local implementations A12 lifted from them.

---

## Guards

In S13's files, as soon as it merges; codebase-wide once S9 and S10 have adopted A12:

- no `create_subprocess_exec`, `Popen`, `subprocess.run`, `os.kill`, `os.killpg` or `signal.` calls outside `child_process.py`;
- no grace or kill-timeout constants outside it.

---

## Tests

- **Behaviour, on all three CI platforms:** a child that spawns a grandchild is completely gone after a `BoundedRun` times out, and after an `AttachedChild` is stopped. One test per shape.
- **Identity:** a `DetachedProcess` whose recorded start time no longer matches reports not alive, and a signal to it is refused.
- **One new-case test** for A12 (T2).
- **Delete** the old per-module tests of hand-rolled supervision once their modules use A12. Do not port them.
- The existing tests of the lifted implementations' semantics (namespace containment, the bounded authority deadline) keep passing against A12.

---

## Done when

A12 is merged; every spawn, stop and liveness call in S13's files goes through it; `_process_alive` and `_signal_local_owner` no longer exist; the guards pass; owners have been relaunched.

## Dispatch

> **`refactor-s13`:** Complete S13 per `docs/refactor/round2/S13-child-supervision.md`. Read `00-RULES.md` first. Land A12 first as a new module, lifting the existing `pidfd`, namespace and watchdog code rather than rewriting it; S9 and S10 are waiting on it. Then migrate your three files completely. No bare-PID path survives anywhere.
