# Native evidence resource exception ownership — 2026-10-01

Owner Arendt. Receiving base main1f8bf0ef; urgent follow-up to merged493,
independent of unfinished489 and pending494. No live input replay, restart,
state clear or public mutation.

Six actual sequence319 diagnostics show successful native triage reaching the
ignored-result cursor projection. Original nonblocking wire/bus lock acquisition
raises BlockingIOError. ExitStack throws that consumer exception back through
PrivateEvidenceRead.open, whose acquisition translation incorrectly spans yield.
It becomes NativePiUnavailable, bypassing NativeSourceCursor.advance's existing
bounded contention policy. This is not evidence of a model outage or a missing
native file.

Source ownership first: existing PrivateEvidenceRead owns file acquisition and
file I/O errors; caller lock and settlement errors remain with their original
owners. Acquired NativeEvidenceRead/Scope retains bytes only and must close while
propagating a consumer exception unchanged. Inventory similar resource-yield
translation scopes and close their consumer paths in the same source change.
Do not add retry deadlines, proof/state copies or disable reader borrowing.

Validation LAST: focused resource exception checks plus an installed continuous
private journey with at least three actual native owners simultaneously finishing
triage, cursor advancement and bus publication. Provider only may be controlled.
No acceptance result claimed at this work-start checkpoint.
