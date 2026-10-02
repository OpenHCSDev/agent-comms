# Actual #openhcs channel timing — 2026-10-02

One explicitly authorized human message, sent through the default installed
Messaging.send_user_message operation, using the original public channel and
configured saved agents. No replacement fixtures, synthetic audience, native
input replay or owner restart. Raw original send/timing evidence retained in
parent .release-private/live-openhcs-timing-20261002; source text unchanged.

Message358/c9ef4ce2dcda: testing , please reply tersely if you receive this
Human operation0.828s. All12 audience claims sealed together+3.015s.

| Replying owner | Wire reply after original send |
| --- | ---: |
| compaction499-live-architecture-memory | 56.777s |
| openhcs-helper | 58.779s |
| compaction501-live-architecture-memory | 162.367s |
| openhcs-pr159-viewer-bind-owner | 163.422s |
| openhcs-audit-merged-boundaries | 207.860s |
| openhcs-helper2 | 255.582s |

At the observation boundary, all six currently functioning native owners have
answered, but peer acknowledgement work remains deferred/pending. Four native
owners have original compaction StoppedDrain records; two external-CLI audience
members have no live native session. They cannot be silently counted as replies
or removed to pass the acceptance. The one-minute complete journey fails.

Actual native timelines locate the avoidable delay before model input and after
model output. Mendel read original request logs: helper2 admission92.1s,
merged-boundaries122.6s, PR159 full-answer admission81.8s; model requests3–7s.
DraftCore524 owns the shared exclusion/admission source trace and fix. Arendt
owns selected-summary recovery across the four blocked agents. Neither lane
replays uncertainty or hides errors by rewriting original outcomes.

This run verifies a failure on the user's real workload. It is not a fix or a
performance acceptance result. Repeat only after the owning mechanism changes.

Later canonical observation: all30 peer-reply claims among the six functioning
owners settled. Last settlement occurred340.105s after the original send.
Four blocked native and two absent external owners remain pending; the complete
audience journey still fails. No second test message or retry was sent.
