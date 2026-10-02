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


## Installed523/524 repeat — original365 (2026-10-02)

The same user-authorized text was published once after the new default installed.
Original365/d5e82a0ffd5f, wire timestamp1790936041.144690. Human operation0.901s;
all12 claims accepted at+2.073s. Six actual configured native replies arrived
at51.245,52.934,58.329,63.280,63.912 and66.102s. Their30 mutual claims all
ended ignored by+109.348s. This improves the prior255.582/340.105s result but
fails the complete one-minute target. Two absent external participants remain
explicit; four originally blocked native owners did not answer this original.

Those four old reservations are now retired through523 with old input proofs
preserved. Fresh mandatory operations hit source-change continuation errors;
four later summary/native operations committed, while365 originals remain
unadmitted/deferred. Arendt owns the source/commit/admission consumer closure.
No failed input was replayed and the test message was not resent.

Mendel joined365 to original native inputs: provider/native windows3.1–6.3s,
TRIAGE completion14.8–19.7s, FULL starts35.3–44.6s, native FULL finishes41.7–49.7s,
wire51.2–66.1s. Progress reaches parent diagnostics4.6–16.3s late; inline
SelectedParticipant registry phase writes sit inside native event draining.
Draft528 removes transport-sample mirroring through the original TurnSession
consumer family; it is implementation in progress, not a measured new speedup.

Raw original receipts stay in parent .release-private/live-openhcs-timing-524-20261002.
Installed318 preservation publication43.253s/all19 process identities/five links;
actual ordinary default helper startup28.706s, final Ready/history reviewed,
owned observer cleanup empty. Source changed inbox warnings were visible and
remain part of the evidence, not omitted from readiness.
