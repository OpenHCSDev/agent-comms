## Deletion and ownership

328 production lines deleted;543 added. GoalScheduler owns the existing ledger/origin/projection/control state removed from TurnRunner. Shared ReservationRule declarations own independent goal/wake barriers; RegistryOwner, ProcessIdentity, GoalRevision and lifecycle declarations own exact authority. QueuedInput owns one acceptance context and the live handoff/future-receipt behavior. All callers migrated; no aliases, new storage or codec subclasses.

Source **6a456105**, merged current main363/364 **11dcae27**. No overlapping implementation in selected/coordinated runtime/startup. Parent owns live install.

## Evidence

- **8 passed21.75s**, actual noneditable installed current-main package: saved-history ACP -> failed goal -> socket Retry -> scheduler/wake -> native success and exact fourth journal input; retained UNKNOWN unchanged. Also actual selected+queued fresh input; READY fences including same-PID changed birth and valid alias rename; UI grant controls.
- **10 passed16.23s** includes actual retry and unrelated-turn success/error/EOF/exception/cancel admission without overlap.
- Ratchet passes: **35 fewer chain terms, six fewer long conditions, eight fewer foreign-absence probes**; class excess above500 falls229. No long conditions remain in runner/drain/new owners.
- Original RED receipts retained; stale private-session setups and ineffective post-capture key mutation tests fixed at actual authorities. No production guard weakening.

[Full receipt and remaining scope](evidence/s14-goal-scheduler/README.md). No diagnosed source/caller blocker for this slice. Ready for parent integration/live verification; CI deferred. Other S14/T4 scopes remain assigned. Latest NRA/refactor-audit/global instructions applied: IDEN-1/3/8, IMPL-14, TIME-3.
