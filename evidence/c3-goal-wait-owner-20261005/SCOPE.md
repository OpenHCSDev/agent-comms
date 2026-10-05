# Dependency wait current-owner decision

GoalWait.current_for owns applicability to an owner incarnation and delegates
identity/revision to the existing Goal.accepts_observation. Four copied policies
are deleted: wait-graph node, wait-graph peer, closed-wait recovery and terminal
turn recovery. Optional lookup, process/turn custody, status, locks, reply reads,
write ordering and clear semantics remain with their existing owners. Reply
consumption intentionally retains its incarnation-only check.

The four callers already select waits by goal ID. The delegated check also
refuses a malformed lookup containing a row for a different goal. Later goal
revisions retain waits; future wait revisions and replacement owners refuse.
No stored fields, codecs, runtime schema, migration, native operation or input
lifecycle changes.

## Verification

Existing Repository/Package source read: 324 production,369 test,54 tool modules,
zero omissions. Before source sites are recorded in source-before.json.
Five authored offline wait-graph cases PASS in1.97s using host Python3.14 and
original Comms/Registry/GoalWaits on new private authored stores. No native,
ACP process, SDK, provider, original session, public root or installed prefix
was used. Test scratch is removed after terminal.

The first existing-test command stopped before collection: host lacks the
configured xdist plugin. Serial existing standby modules then stopped during
collection with ModuleNotFoundError: acp. No dependency installation or borrowed
holder followed. The first authored graph control had five assertion failures:
it expected terminal leaf nodes in the group instead of only waiting owners.
Its exact source is preserved in control-before-graph-result-correction.py;
only expected group membership changed for the final passing run. Production
was not altered in response to those authored expectation failures.

This is source qualification only. Existing installed ACP terminal/recovery
checks still need a fresh matching installed-source purpose; no live-readiness,
performance or complete six-file C3 claim is made.
