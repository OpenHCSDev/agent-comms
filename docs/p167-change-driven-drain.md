# P167 — change-driven drain recovery

Owner: core-read-locks. Base: merged main d8b72586c.

Source pass: drain_failures, InputObservation, SelectedWakeCheck, AgentActivity,
DrainReadiness and WireWatch. Existing readiness owns diagnostic provenance;
AgentActivity owns fenced recovery. The clean-path worker dispatch is redundant:
its existing recovery guard already prevents a clean-path activity write.

Repair: keep that guard at AgentActivity before worker admission; remove clock
retry and stop successful-wake callbacks from waking failed/cancelled work.
Expected availability failures record once and reach the observation loop, which
then waits for its original file/dependency signals. Programming errors stop.
No new timers, counters, caches, tests, provider input or uncertain-input replay.

P107 is frozen/reassigned to toad-theme; no P107 files are part of this repair.
