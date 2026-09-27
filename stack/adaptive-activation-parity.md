# PR95 adaptive activation parity — design gate, not an opt-in

Status: **BLOCKED**. The default-off ACP constructor candidate and exact native
commit are structural tests, not permission to use a provider or deploy. No
production worker or stdio `CommsClient` enables adaptive compaction.

## Concrete mismatch at the selected live Pi process

The ordinary owner backend launches/reuses one Pi RPC child with a pinned
`agent_bin`, model-adjusted RPC arguments, worktree cwd, complete launch env,
auth-file revision, session identity and in-memory selected state
(`backend.py:840-951`). Its output model/usage is an observation, not a
credential or route attestation. The current adaptive summarizer creates a
**different** Node process (`owner_compaction_provider.py`) after reading the
saved session. It strips ambient Node options, launches in the session-file
parent directory, creates a new `ModelRuntime`, resolves a model by provider/id
and obtains new auth; it cannot prove equality to the idle Pi child’s selected
model instance, baseURL, credential/header route, loaded extensions, CLI
options, trusted project configuration or transient state. Comparing only
`owner.model`, reported `runtime_info.model/context_size`, and global settings
cannot bridge this gap. Project settings and custom model declarations currently
cause a fail-closed trigger skip; absence is source-captured and rechecked.

**Do not add an environment-variable or public ACP opt-in** to paper over this
mismatch. A detached child cannot attest what the live selected Pi would have
done. Hard-context protection remains native and independent even when the
adaptive trigger skips.

## Proposed proof boundary for a later change

1. Specify a read-only, bounded summary operation on the **already idle,
   selected Pi process**, not a detached `ModelRuntime` approximation. It must
   capture exact selected model/credential route and effective settings/trust
   inside that process without writing a compaction entry, loading new
   extensions, dispatching tools, changing selection, or sending original
   input. A new native patch/RPC API requires its own exact byte pins and
   independent review; feasibility is not yet established.
2. Capture canonical owner/turn/goal, original unbound `acp:` input and native
   source **before** any provider request. Keep the owner ACP turn lock and
   executor exclusion while asking the idle process for one bounded summary;
   reject changed source/model/settings/route at handoff. Provider work is
   single attempt with timeout/output limits and no retry. An uncertain
   selected operation blocks the turn; it is never treated as a clean skip.
3. Retire and reap that same idle manager **after** summary and **before** the
   external native journal/CAS writer. Preserve cancellation join, retained
   authority, exact commit ID, pending metadata-only outbox and strict fresh
   validated reopen before the original input’s ordinary send. No provider
   summary is broadcast or replayed; no pending/UNKNOWN input is borrowed.
4. Only after provider-free parity/denial tests, real semantic-retention
   evaluation, full isolated source+wheel suites, independent combined review
   and live main/PR recheck should an authenticated operator-facing opt-in be
   designed. Worker `CommsAgent(..., runtime_enabled=True)` and stdio
   `CommsClient(..., runtime_enabled=True)` both remain default-OFF now.

## Minimum provider-free acceptance matrix for the future RPC seam

- Wrong selected model, live credential/baseURL/header revision, CLI/extension
  route, project trust, or settings revision: no provider request/commit/send.
- Correction/goal/owner/session/input movement during preparation or summary:
  no native commit and no original-input replay; prior UNKNOWN remains UNKNOWN.
- Provider failure/timeout/cancellation and split-turn preparation: no retry
  and no native mutation; after selected uncertainty, the owner turn blocks.
- Successful synthetic summary: retire old child before one native CAS; exact
  fileOps/usage digest and receipt match durable intent; one metadata-only
  projection precedes one original native start and strict reopened source.
- Post-CAS transport loss, owner cancellation/SIGKILL and publication partial
  delivery: exact-ID reconciliation only, pending-or-observed local outbox,
  surviving exclusion/join and no duplicate provider or input dispatch.

The exact `06dc706` metadata-binding correction is under narrow independent
review. It does not clear any of these selected-model or deployment gates.
