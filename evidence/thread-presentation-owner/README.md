# Targeted thread presentation

One thread read now uses the existing ThreadView declaration instead of constructing
every other thread's view. ThreadView owns eligibility, the authority join and roster
construction. The replaced HistoryViews._thread_views_for and every caller are
deleted; bulk viewer snapshots and individual reads share that implementation.
AgentActivity accepts the existing captured registry snapshot, preserving the owner
incarnation and lease used for activity projection. No new cache, store, codec,
compatibility alias, state family or startup input replay.

Actual noneditable installed core, focused current lifecycle/notification/activity
cases: 18 passed in 3.06s. Earlier invocations stopped on missing native fixture
environment or missing disposable basetemp parent; neither was a product failure.

Read-only installed check against the actual current live root: all 102 visible
thread presentations exactly equal the existing roster interpretation plus actual
recent assignment notifications. Zero provider requests and owner restarts. Missing
and archived threads remain absent; stopped threads, aliases, executing owners and
current goal projection preserve the canonical interpretation.

Toad's read_thread_presentation caller migration and its continuous installed native
journey belong to Tesla's existing performance continuation. Until that pair is
verified this API is not claimed live or as final latency completion. CI deferred.
Parent owns final paired default installation. Latest NRA/refactor-audit and owner
decisions followed: existing declaration ownership, full caller deletion, unchanged
freshness boundaries and no second metadata/cache mechanism.
