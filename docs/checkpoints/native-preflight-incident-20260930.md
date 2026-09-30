# refactor-r1 original cold initialization failure

Owner: Arendt, full S14. Source is actual installed Core000/Toad173/nativee36 after
first441 ship. Public root and failed attempt are preserved; no automatic retry,
Inbox drain restart, goal resume, signal or user input has been performed here.
This checkpoint is forensic evidence, not a completed production fix.

## Original input and producer ordering

Private diagnostic5a71d2b95ecda3268ea166ff25496e1b refers to wire160, an original
2026-09-28 nra-domain-mapping to #nra bounded-triage channel delivery. It is not
a fresh user submission. NativeRuntimeInput holds its reserved TRIAGE input;
sent-owner admission, session/input-entry/context receipt and verdict remain
absent. That durable reservation remains UNKNOWN. Absence by itself is not
no-byte proof or replay permission.

The installed traceback gives the additional original control-flow witness:
TrackedTurnSession.complete failed inside attest.next_event, before admit_prompt.
Only the correlated get_state control was sent; the one-use prompt send boundary
was never entered. The reported elapsed preflight wait is10.046s and no input
started. The future canonical initialization/command witness must carry this
no-prompt distinction through failure settlement and UI feedback. Do not parse
the phase prose or relabel this historical reservation without review.

## Verified original timestamps

| Event | UTC2026-09-30 |
| --- | --- |
| Original carry proof finalmtime |08:51:31.324505 |
| New refactor-r1 process start, /proc ticks projection |08:51:46.909 approximate |
| Operator stdout final13-launch resultmtime |08:51:55.228487 |
| R1 preparing old delivery |08:52:19.561679 |
| R1 checking old delivery |08:52:19.567983 |
| Native failure/idle |08:52:30.409503 |
| Stopped drain publication |08:52:30.458684 |
| Parent corrected durable active route, filemtime |09:01:05.345917 |

Existing441 prints the13-launch result AFTER canonical wire custody exits.
Native checking starts24.339s after that final result. The original launch-batch
wire-held interval therefore did not directly overlap this attestation failure.
Operator2821553 and writer2821684 are gone. Parent's transient original lock
observer and disk-read validation evidence are retained privately; no preimages
or credential-adjacent records are published by this receipt.

R1's actual process2824066/start23394608 remains alive, active_turnNone, no native
child, session_fileNone; current owner generation1008 and admission972 are
different domains. At observation it had9.29s self CPU and4.74s accumulated child
CPU. Those totals do not identify native startup's wait resource or PID.

## What is and is not established

Tracked TRIAGE launches explicit verifiede36 from its worker environment using
raw node CLI, no tools/extensions/skills/context files. It does not use managed
project-bootstrap imports. The default route still selected4ab during the failed
attempt, but this native launch had explicit correcte36/rootID. No evidence shows
that default-route mismatch blocked this no-extension native initialization.

Only R1 has preparing/checking activity in the bounded08:51:40–08:52:50 window.
Its two retained native files have lastwrites01:45:14 and01:45:40 UTC. There is no
new input/context journal receipt at the incident time. Journal persistence can
be lazy, so that absence does not locate the native startup stage.

The exact native child PID, wait channel, command timing, partial stdout and
stderr were not retained. No native RPC debug log was configured. The installed
tracked cleanup gathers the bounded stderr tail and then discards it on timeout.
That is a concrete diagnostic consumer gap. The tracked start path also bypasses
existing NativeStartupAdmission, used by backend and prepare. This is a concrete
ownership gap, not proof that contention caused this specific failure. No SQL
deadlock, provider wait or extension lock is claimed without native IPC evidence.

## Next owned implementation and actual acceptance

Reuse the existing NativeStartupAdmission and original native child/attestation
custody for ALL cold callers. Keep readiness timing distinct from whole-turn
duration; do not increase10s or add a progress mirror. Preserve bounded child
stderr/command evidence through existing diagnostics, with nominal pre-prompt
failure disposition and no automatic recovery of this original attempt.

Reuse actual native initialization fixtures for a private R1 configured-model
setup, get_state/attestation only first, followed by real startup/control failure
and noReplay coverage. Source resolver gives openrouter/z-ai/glm-5.3-flash; do not
substitute a paid model or reuse the original failed message. Current resource
check reports18.2GiB available RAM but critical16.4GiB swap. No extra fleet or
large build/test is started; reduce resource pressure before a large native run.
No private441 receipt/preimage is deleted. The small capture fixtures and source
worktree remain protected. No source patch claims the missing first cause.
