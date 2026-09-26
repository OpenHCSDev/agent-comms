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

## This slice closes (was an explicit gap)

| Gap | Now provided |
| --- | --- |
| "No versioned prelaunch source/claim/stage/input/prompt digest binding exists yet" | `native_prompt_binding.py`: versioned sidecar store writes `expected_prompt_digest` (sha256 of exact prompt bytes) bound to source seq/message, sealed claim, stage, input ID, and owner incarnation **before Pi launch**, under a live owner recheck |
| Crash/UNKNOWN ordering untested for that binding | Binding commits after reservation and before launch; crash anywhere before launch leaves the input unprovable; tests cover owner-change between reserve and bind, launch failure after bind, digest mismatch, and immutable bindings |
| Historical evidence could not establish expected-prompt equality | `read_historical_native_inputs` joins the immutable binding to the private journal's durable `inputDigest` and sets `expected_prompt_equality_established` only on exact match; identity mismatch fails closed |

## Still open (unchanged by this slice)

- **Ordinary `comms_send` → private N/K bridge.** Ordinary sends still never
  enter the private cohort/claim stores; the selected runner only sees
  `send_initial_cohort` originals.
- **Injected-message cursor.** No per-recipient proven-injected cursor exists;
  binding equality is necessary but not sufficient for a cursor (canonical
  assembled-context acceptance semantics are still pending upstream proof).
- **Every-wake injection service, decision/obligation projections, supersession,
  task heads** (proposal §§3–4): not started.
- **Mediated pre-write admission gate** (proposal §5): not started; legacy
  independent explicit claims remain valid; shell/child writes are unenforced.
- **Append-driven index refresh, deadline/stress numbers at 10/150 threads.**
- No provider calls or live activation were used for any of the above.