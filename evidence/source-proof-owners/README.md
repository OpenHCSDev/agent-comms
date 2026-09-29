# S7 canonical source proof and current cursor ownership

Parent assigned after301 main300 closure. Worktree
`~/wt/comms-source-proof-owners-20260928`, branch
`refactor/source-proof-owners-20260928`. Scope claim coordinated on Wegener303:
https://github.com/OpenHCSDev/agent-comms/pull/303#issuecomment-5881471576
No backend/watchdog/native provider edits or live installation. Parent owns install.

## Ownership and deletion

- NativeSourceCursor owns one bus/coordinator/root observation and monotonic write.
- CursorOwner captures thread, participant generation and admission generation;
  it owns the exact live-identity comparison and common immutable native-receipt
  identity check. Capturing the value grants nothing: reads and writes still
  recheck it under canonical wire/bus/registry/SQL boundaries.
- SourceCoverage owns recipient-bound canonical paging, cohort receipt matching,
  historical input evidence, activation floor and bounded pass state. Whole-prefix
  proof remains mandatory; persisted high-water is never used to skip a prefix.
- Existing WakePolicy declarations compose native stage requirements from the
  public SourceProofRequirement ABC; TriageSourceProof inherits the shared FULL
  evidence check, with no independent names, tags, registry or policy inventory. No second
  policy registry, enum switch or mirrored policy inventory was introduced.
- Deleted the old free read/advance/paging/proof helpers and migrated all callers.
  No compatibility aliases or exports. Deleted the obsolete retained_probe script
  naming the removed D22 staged candidate; retained historical receipts remain.
- Tables, stored formats and external Pi contracts unchanged. UNKNOWN gaps, missing
  receipts, old generations and unbound journals do not become current proof.

## Dependencies / evidence

SourceCoverage reads existing cohort and historical-native owners; policy refers
only to the typed historical evidence under TYPE_CHECKING. CursorOwner consumes
registry/participant/native row identities, and NativeSourceCursor composes both.
No schema bootstrap, input resend, claim acceptance or recovery is added to reads.

Source-proof guards enforce removed-call closure and the S7 function/module
bounds. Actual installed verification is complete below; no paid provider or CI wait.

NRA before scan used complete src/agent_comms context, raw/full payload,1 parser
and1 analysis worker,150s internal/165s wall bound. Two semantic-mirror leads point
to the duplicated live-owner comparison; their proposed DeclaredFamily compaction
members do not own registry identity. The actual captured owner is chosen from
that dependency analysis. CLI has no detector omission inventory in its payload;
manual edits are not claimed NRA-certified rewrites. After receipt follows.

## Final ready evidence

Integrated current main664623a3, including301,300,305 and Wegener303. No source
conflicts or overlapping backend ownership. Actual installed wheel, not editable
source, used for each recorded native run.

- initial-focused.log:88 passed,2 skipped in144.28s. Real SQLite/canonical files,
  >100-addressed-source and150-recipient cases, forged high-water, old admission,
  substituted bus, UNKNOWN gaps, full-prefix and mid-read generation fences. The
  initial skips needed native-package configuration, covered by native runs below.
- installed-native.log:9 passed,1 skipped in20.74s, including3 actual selected
  executions, fresh/retained ACP attachment/bootstrap and2 guards. Skipped digest
  micro-test used a different environment name, then exposed the defect below.
- affected-callers.log:46 passed,2 failed. Fixed the leftover rename-test injection
  to ParticipantStore.advance_generation. NativeSourceCursor owns no old alias.
  The second failure was reproduced against the pre-refactor installed branch
  (baseline-cursor-refresh.log): PR299 intentionally republishes unchanged proof
  after contention invalidates the local announcement. Updated the assertion to
  require exactly one renewed projection, identical observation and later revision.
- native-digest-and-rename.log preserves the first native digest failure: it called
  a real prototype method with a fake partial session object, so current Pi correctly
  lacked its entryStore. Deleted that obsolete48-line test. The same assertion now
  executes in the real native selected-execution test: recorded request digest must
  equal the durable prelaunch binding and the corroborated input must establish
  prompt equality. No extra fake context or native implementation change.
- native-digest-current.log:7 passed in12.73s, including3 actual native selected
  cases with the new digest assertion, real flock recovery and the guards.
- final-capabilities.log:89 passed in23.41s, combining declaration families,
  full/triage/no-wake behavior, SQLite/native prompt binding, the3 real native
  selected cases, and ownership guards after native-proof capability composition.
- combined303-native.log:5 passed in11.25s after the final main/303 merge: all3
  actual pinned-native selected cases (ordinary direct, post-activation-floor,
  selected write), plus both ownership guards. Each native case performs actual
  CLI launch, local HTTP/tool execution, native journal digest corroboration,
  current cursor read, response publication and owner release. No paid provider.
- ratchet-final.json:zero positive deltas including every tracked per-class size.
  The intermediate ratchet.json is retained with4 policy growths; replaced direct
  policy implementations with actual shared proof capabilities: engaged triage
  inherits FULL proof, with no transient FullWake construction or duplicated check.
  No exemptions, baseline reset or metric changes.
- Source module sizes215/182/254/43; largest methods53/42/64/8, respectively.
  Production591 added640 deleted (net-49). Tests and final exact metrics are in
  structure.json; guard and actual-native assertions account for test additions.
- NRA final scan completed8.862s with full dependency context. The two previous
  duplicated-owner-comparison leads now collapse to one on CursorOwner.require_live.
  Its proposed compaction/Pi declarations do not own registry incarnation facts;
  preserving the explicit owner comparison is deliberate. No claim of global
  coverage or certified equivalence; raw lead and CLI limits remain recorded.

No remaining diagnosed source/caller/fence blocker in this slice. Parent owns
merge/install. Other S7 surfaces remain outside this PR.

## Commands and cleanup

Install: `uv venv .venv` then `uv pip install --python .venv/bin/python '.[dev]'`;
subsequent source revisions used `--reinstall-package agent-comms .`.
Pytest runs use `-o addopts='' --basetemp=<owned-root> -q`, without CI or parallel
provider calls. The native package environment was:
`AC_NATIVE_COPIED_PACKAGE=/home/ts/.local/share/agent-comms/native-current-5fdef596596173bd/node_modules/@earendil-works/pi-coding-agent`.
Final combined selection: test_selected_execution_native and
 guards/test_source_proof_ownership. The larger capability selection also includes
 test_wake, test_s3_legality_behavior and test_native_prompt_binding.

Completed301 derivative roots removed. This branch's scratch/NRA cache, test roots
and virtualenv are removed after workers exit; all red/green receipts and source
remain in the persistent worktree. No live roots or predecessor source changed.
