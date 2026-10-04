# Original407 SQLite resource closure

Source: Core8f0e4d92e93000c4c7b7bf83e4da56544c10eb66, installed330.

Original input407/4007d9fb94f0 was sent once. Actual UI failed about15seconds later. The preserved crash traceback wraps an original SQLite OperationalError at TranscriptPublication.suspend/Worker.wait; it does not identify the original SQL statement. Terminal st log is not that traceback.

Owner: Mendel. Whole Core coordination SQLite construction/configuration/read/write resource relation, including fresh536 workers and canonical native/source/handling read consumers. Sch owns receiving packaging; Arendt grants disjoint Core resource scope, Einstein retains fork/compaction source work. No public writes/stops/provider/input replay.

AST before: existing NRA Package parser, production/tests/tools complete. Dotted calls and nested declarations require semantic reading; counts are leads, not dynamic resolution proof.

First source facts: CoordinationStore performs schema read and synchronous configuration for every fresh worker connection. NativeRuntimeInput._publication_read and NotificationAssignment.select independently open read-only connections with50ms budgets, and pass raw SQLite contention to transcript consumers. Read the full owners and transaction lifetimes before implementation. No timeout patch or busy-error suppression.

Patterns: IMPL-13 resource lifetime; BOUND-2 existing owner; IDEN-7 read custody. Checks last, scoped to the coherent source change and original failure. Current407 continues independently; no repeated provider/capture.
