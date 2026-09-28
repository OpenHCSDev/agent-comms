# Observable drain failure — ready

Implementation: a2f57bffc5fcefd55bad760062042c604523db00. Merged current main afe0688a (deployment metadata only) without changing native_pi.py.

- Missing base-only runtime DB and drifted cohort schema: actual background InputDrain emits an owner-scoped diagnostic through the existing activity log and ThreadView. Explicit restoration of the current schema clears the diagnostic and returns Ready.
- Diagnostic persists across stale activity expiry and unrelated turn completion. Repeated watcher passes neither flood the log nor repair schemas. Replacement owners do not inherit previous diagnostics.
- Unexpected exceptions record a stopped diagnostic and propagate; expected I/O/schema boundary errors stay visible while observing recovery.
- No parallel status store, repair, prompt replay or new native proof path.

## Evidence

Installed wheel (noneditable): `python -m pytest tests/test_live_drain_failure.py tests/test_private_idle_drain.py tests/test_activity_index.py -q` — **17 passed in 21.96s**, installed.log. This runs the actual SQLite/private ACP drain; the unexpected TypeError regression alone injects an implementation failure to verify fail-loud behavior.

Paired Toad PR128: installed wheel on current T4 main 08d464b, two affected mounted pilots passed (45.95s), including native conversation + DM warning, ACP Ready preservation, and recovery. No provider call or live-root mutation. Core source is ready independently; UI deployment needs paired Toad caller changes. CI deferred.
