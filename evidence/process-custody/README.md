# S13 process custody closure

## Ownership and deletion

Boyle owns `child_process.py` process custody and canonical lifecycle/test callers.
Claim posted on PR383 after bounded review of Wegener19bdffd9 (READY). Parent owns merge/install.
S13/A12 current closure extends existing custody: no new process supervisor, registry, or native proof format.

- Delete `DetachedProcess` optional-Popen representation, all `attach` factories and callers.
- `ParentedProcess` owns OS child handle, reap result and pipe retirement. `ObservedProcess` owns recorded-incarnation observation/signalling and cannot claim a parent's exit code. Shared stop algorithm remains `ChildProcess`.
- `ChildLaunch.spawn` is the single synchronous gated creation. Deadline and normal launches consume it.
- `InheritedDeadline` owns watchdog/pidfd acquisition and reverse-order retirement; delete nullable child/watchdog/pidfd cleanup roster and foreign stream iteration. Child retires before watchdog; inherited authority descriptions are never unlocked by cleanup.
- All production callers, tests, executable evidence probes migrated; no aliases or old API.

Latest global AGENTS, NRA, resolved exact refactor-audit and S13 reread. Patterns: IDEN-3 optional custody state, IDEN-8 exact process identity, IMPL-13 one process mechanism, TIME-3 deletion without compatibility, AGENT-6 ownership rather than relocation.

## Evidence / pending

`actual-child.txt`: 22 PASS24.06s, actual OS children/process groups/pidfd/inherited locks, repeated cancellation, mismatched identity refusal, owner-only reap result and exception pipe cleanup.

Installed native commit/UNKNOWN and actual owner restart/caller gates pending. No readiness claim, no live changes. No native package edits. CI deferred.

Owned disposable `.venv`, `.installed` and `.scratch` will be removed after receipts. Source/receipts retained.
