# PR17 claim-admission acceptance-gap checklist (audited 2026-09-26)

Audited against merged `main` at `d0ced47` (which contains PR #17's merged
slices, PR #89 ordinary-DM goal context, and PR #91 blocked-goal reasons) and
`docs/proposals/seamless-claim-admission.md`. Each row records the exact seam
that was inspected. Nothing here claims live provider/model acceptance.

## Done (merged, reviewed)

| Contract item | Evidence seam |
| --- | --- |
| Private selected N/K framing of one verified original/claim; triage/FULL wording; no-wake zero model calls | `coordinated_runtime.run_one_sealed_claim`, `wake_injection.render_selected_wake_frame`, `tests/test_coordinated_runtime.py` |
| Versioned `WakeAdmission` verification against committed original, sealed claim, owner generation/turn, execution, unfinished attempt | `claim_admission.py`, `tests/test_claim_admission_verifier.py` |
| Test-gated single existing-file claim publication, UNKNOWN post-append semantics | `operations.publish_selected_resource_claim` |
| Disposable versioned WAL candidate index (v1 refuse, bounded v2 rebuild) | candidate-index modules + `tests/test_cohort_schema.py` |
| Read-only historical triage/FULL evidence joined to live-recorded Pi context proof | `historical_native_inputs.read_historical_native_inputs` |
| Reserved `native_runtime_inputs` row before Pi launch; no automatic replay; crash leaves unproven input | `coordinated_runtime._reserve_triage/_reserve_full` |
| Ordinary direct-DM wake into an active goal (no goal permit/wait consume) | merged `fbeab39` slices |
| Goal-block reason contract (mandatory reason, refusal persistence, retry clears) | PR #91 (`1cd1211`,`3d755dd`,`fa1cda0`) |

## Unaccepted checkpoint: corrections in progress

The original `9e89a91` checkpoint failed parent review: its bare-text digest
was incompatible with native Pi and its owner check did not span launch.
The following is partial implementation evidence, not closure or approval.

| Gap | Now provided |
| --- | --- |
| "No versioned prelaunch source/claim/stage/input/prompt digest binding exists yet" | `native_prompt_binding.py`: versioned sidecar store writes `expected_prompt_digest` (sha256 of the native `pi-input-request-v1` JSON request envelope) bound to source seq/message, sealed claim, stage, input ID, and owner incarnation **before Pi launch**. Digest is cross-checked against a compiled native method. Binding insertion holds the coordination transaction. A separate one-use callback holds wire/bus/registry/coordination exclusions at actual raw-pipe prompt writes on an isolated writer thread |
| Crash/UNKNOWN ordering untested for that binding | Binding commits after reservation and before launch; crash anywhere before launch leaves the input unprovable; tests cover owner-change between reserve and bind, launch failure after bind, digest mismatch, and immutable bindings |
| Historical evidence could not establish expected-prompt equality | `read_historical_native_inputs` joins the immutable binding to the private journal's durable `inputDigest` and sets `expected_prompt_equality_established` only on exact match; identity mismatch fails closed |

## Still open (unchanged by this slice)

- **Owner/generation fence through actual native send: successor review pending.**
  Independent review rejected `96b71e0` (also present at `3a22119`): synchronous
  admission held across `await stdin.drain()` deadlocked when an ordinary
  same-loop lifecycle callback waited for that registry lock. Green subprocess
  exclusion tests did not prove event-loop liveness. The successor uses a
  dedicated raw writer thread, thread-local SQL connection and nonblocking
  canonical wire/bus/registry/SQL/sidecar admission. Every `os.write` is inside
  the scope; no StreamWriter prompt buffer can flush after release. A monotonic
  writer deadline capped at five seconds progresses independently of the owner
  loop. Partial/error/cancelled sends stay UNKNOWN/unreplayable; cancellation
  joins the writer and shields child reap from repeated cancellation.
  Twelve focused admission tests include real pipe backpressure, synchronous
  same-loop unregister, send timeout, repeated cancellation, suspended preflight
  drain, real child reaping, subprocess exclusions and stale-owner refusal.
  See `docs/native_prompt_send_admission.md`. This proves neither native
  acceptance nor provider receipt; full N/K/write admission remains incomplete.
- **Private sidecar safety/durability: implemented, review pending.** SQLite now
  opens only `:memory:` and deserializes bytes from a pinned no-follow regular,
  owner-only, single-link file descriptor. It never reopens the pathname for SQL.
  A pinned-directory snapshot lock serializes installers/readers/writers. Every
  schema object (not only prefixed names), metadata, and snapshot integrity are
  checked on the exact connection used. Binding insertion requires rowcount=1
  and exact full-row readback. Changed snapshots publish via fsynced staging,
  atomic replacement, and directory fsync; a durable intent blocks automatic
  reuse after interrupted publication. Any failed durability receipt denies send.
  Final intent-removal fsync can fail after data is already durable; later reads
  remain informational and cannot retry a reserved input. Tests cover both
  parent's concrete negatives, path/lock substitution, alias/mode/owner drift,
  schema drift, suppressed binding inserts, every fsync point, concurrent
  installers, and real SIGKILL before/after replacement.
  **Limitation:** bounded 32 MiB pilot snapshot store, full-file rewrite per
  changed commit; not a scalable append database. All cooperating writers must
  use this protocol; arbitrary same-uid code and shell/child writes are not an
  OS-enforced sandbox. Capacity/performance remains an explicit integration gate.
- **Ordinary `comms_send` → private N/K bridge: partial integration.** On an
  explicitly marked private root, ordinary tool sends now reach the existing
  full-N/K publisher, and the existing foreground consumer seals and executes
  selected sources (direct/channel FULL, triage IGNORE/engage, no-wake).
  Unmarked public roots are unchanged; no historical audience is inferred.
  Seven new pipeline cases plus adjacent suites: 212 passed, provider-free.
  See `docs/ordinary_nk_delivery.md`. Proven cursor, normal ACP/session wake
  wiring, mediated writes, alias/human paths and scale acceptance remain open.
  The earlier record-only candidate prototype is still non-authoritative and
  is not used to admit these ordinary sends.
- **Injected-message cursor: bounded historical coverage only.** The live
  runner now checks the exact prelaunch binding against the native journal
  digest *before* recording context evidence; a mismatch leaves the reserved
  input unproven and never retries it. `proven_source_coverage.py` read-only
  walks at most 100 canonical private initials, verifying sealed full-N/K
  receipts and each selected source's live-recorded context/binding/journal
  equality. It stops at the first unaccepted, UNKNOWN, PASSIVE or incomplete
  selected source even if a later source has proof; it distinguishes sealed
  no-wake receipts from injections. **Its `covered_seq` is not an ACK or a
  current injected-message cursor** and cannot authorize skipping work,
  provider receipt, writes, or responses. A durable current-owner/session
  cursor and unbounded/scale path remain open. This delta has not inherited
  either earlier independent review.
- **Every-wake injection service, decision/obligation projections, supersession,
  task heads** (proposal §§3–4): not started.
- **Mediated pre-write admission gate** (proposal §5): not started; legacy
  independent explicit claims remain valid; shell/child writes are unenforced.
- **Append-driven index refresh, deadline/stress numbers at 10/150 threads.**
- No provider calls or live activation were used for any of the above.