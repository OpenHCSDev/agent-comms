# Actual public channel sequence 326: lifecycle and timing

Read-only original witness join, not an installed acceptance run. No native
process, input, replay, restart, repair or public writer was invoked. The
analysis uses the live Native5 SQL shape directly; unfinished #489 Native6
declarations are never installed as a reader of the original store.

Original message prefix `6ba2aee585ff`: “testing , please reply tersely if you
receive this”. All twelve assignments were accepted at `1790906166227`.
Ten exact triage source memberships join to ten original native proofs: nine
`FULL`, one `IGNORE`. Two assignments remain triage pending. The nine FULL
claims remain deferred and have no full execution. Singer owns the original
source/assignment/ExecutionStore handoff repair; #489 does not duplicate it.

The open-prs native assistant actually returned `{"decision":"IGNORE"}` with
`stopReason=stop`. Its ignored claim is the model's successful bounded decision,
not a failed FULL attempt or a UI invention. This receipt does not validate that
decision against the user's explicit request.

## Measured clocks

| Owner | Claim → native preparing (s) | Header → original user (s) | Model request (s) |
|---|---:|---:|---:|
| audit-merged-runtime | 7.187 | 3.568 | 7.444 |
| audit-merged-boundaries | 10.677 | 2.475 | 5.356 |
| compaction501 | 20.558 | 13.286 | 3.640 |
| architecture-memory | 29.672 | 22.485 | 3.482 |
| helper | 33.190 | 24.879 | 3.685 |
| audit-merged-models | 44.880 | 36.877 | 4.840 |
| audit-open-prs | 52.638 | 47.182 | 7.551 |
| helper2 | 58.817 | 53.150 | 3.287 |
| compaction499 | 69.131 | 61.066 | 3.674 |
| pr159 | 80.483 | 74.730 | 7.033 |

Native request elapsed values are the producer's monotonic measurements. Claim,
header and user timestamps are system wall-clock observations; they do not
substitute for absent physical-lock spans. Kernel child birth → preparing spans
use the same host monotonic clock, with scheduler tick resolution.

The first two model requests overlap: runtime prepares at 6173414 and finishes
6180858; boundaries prepares at 6176904 and finishes 6182260. Later request starts
are staggered. Native callbacks total 3.6–12.8 ms per request. Recorded observation
lag reaches 4.65 s but includes queued consumption and Python publication.

**The large remaining interval precedes native user persistence/model preparation.**
Original get_state-response, writer-grant and individual lock-wait timestamps
were not retained. These records do not identify which acquisition consumed it,
and do not prove a global provider serialization or provider-capacity cause.

## Original deployed ownership and lock scopes

`receipt.json` fingerprints the deployed files rather than assuming that #489's
working source already runs publicly.

| Existing owner | Scope and consumer closure |
|---|---|
| `SelectedParticipant.select` | Wire/source/SQL read scopes end before yielding. Its lease is an exact per-owner registry CAS, retained through completion; not a held global flock. |
| `SelectedExecution.run` | Holds the coordinator connection and participant lease across triage/full execution. Reads/writes are scoped transactions; connection lifetime alone is not SQLite write custody. `_run_permit` belongs to one execution object. |
| `PrivateSendAdmission.execute` | Coordinator connection spans the await; admission's write transaction is acquired separately in the original raw writer. |
| `PrivateSendAdmission._exclusion` / `_response_boundary` | Dedicated writer acquires wire → bus → registry → coordinator → binding → input document → compaction journal. They span durable UNKNOWN marking and raw pipe writing, not provider streaming. Refused probes close all partial custody. |
| `send_fenced_prompt` | Writer completion joins the original thread after admission/fd close; only then can the parent consume native records. The admission wait has no retained per-lock timestamps. No accepted input is replayed. |
| `NativeStartupAdmission` | Four physical startup slots, released at attested get_state before prompt admission; not a whole model-turn limit. |
| `PersistentPiSession` | Original child borrow/retirement lock is manager-local, not shared between independent worker processes. |

The deployed selected launcher explicitly uses `--no-extensions`, `--no-skills`
and `--no-context-files` for this path. Consequently the global project-sync
tool callback cannot explain this particular triage interval. Ordinary saved
selection/configuration closure remains unfinished #489 scope, not qualified by
these fresh selected sessions.

Two bounded read-only decoder profiles used immutable captured bytes and the
installed codec, without acquiring or changing public stores: current registry
283643 B / 0.0922 s; input document 1316052 B / 0.0752 s (profiling overhead
included). They are current operation costs, not historical attribution of the
74.730 s gap or a reason to remove the original checks.

## Remaining operation-level obligation

Use the existing native command/startup/admission and diagnostic producers to
bracket original get_state acceptance, raw-writer grant and its physical
acquisitions. Do not introduce a second phase/queue authority, enlarge a timeout,
or infer NotSent from absent history. Source closure precedes the final configured
pure-channel multiowner journey. Historical seq326 originals remain unchanged.

`analyze.py` regenerates only this sanitized local receipt from original IDs.
It records source hashes and preserves clock/proof limitations explicitly.
