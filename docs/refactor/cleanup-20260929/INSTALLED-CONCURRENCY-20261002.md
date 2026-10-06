# Installed concurrency architecture — 2026-10-02

## Finding and ownership

Read the actual installed Core `74877f2dd108ed211871a098064178eaf3a4fdb5`,
not a worktree that already contains proposed fixes. This is a semantic source
pass, not a new timing experiment. The user now reports reliable delivery but
unacceptable latency. Preserve that durability while removing unrelated exclusion.

Arendt owns the existing resource/publication/admission closure in Core #509.
Kepler owns its source-selection and acceptance consumers in Core #512, using
Arendt's original `Coordination` resource boundary. Heisenberg owns UI preparation
and frame work in Toad #305 / Textual #22. No competing implementations.

## Current topology

A channel publication seals one original audience. Each recipient's owner selects
pending assignments and leases its own turn, then awaits native TRIAGE/FULL work.
Provider work can overlap across owners. Independent processes nevertheless share
one root's wire barrier, bus, registry, coordination database, compaction journal
and input disposition document. Root-wide exclusion during preparation and pipe
admission can make otherwise independent turns appear serial.

| Resource / owner | Actual installed scope | Consequence |
| --- | --- | --- |
| `WireLog.locked`, `certified_read` | `_store_lock(... shared=False)` by default | Bus readers exclude each other and writers. |
| `WireLog.conversation_sources` | Certificate acquisition, row decoding and validation under the bus lock | UI history preparation competes with message admission/publication. |
| `WireLog.full_history`, `context_manifests`, `retained_context`, `claim_projection` | History traversal/projection under the bus lock; some also hold wire and document locks | Reader CPU work lengthens root-wide exclusion. |
| `CoordinationSession` | Rollback journal, `BEGIN IMMEDIATE` for mutations; `BEGIN EXCLUSIVE` for irreversible admission | One writer for the root; readers can delay commit, exclusive admission excludes readers. |
| `TrackedTurn.send` | Synchronous root wire acquisition inside an async method | A contended file lock blocks that owner's event loop. |
| `PrivateSendAdmission._exclusion` | Wire EX → bus EX → registry EX → coordination EX → prompt/input binding → compaction journal EX | All remain held through the raw native pipe write. Pipe backpressure for one owner delays independent admissions. |
| `PrivateInputs.admission` | Input document shared read plus compaction journal retained exclusive custody | The journal's exclusion covers all sessions despite the admission referring to one exact session. |

`_response_boundary` calls the wire barrier “shared” in its docstring, but its
actual `_store_lock` call does not request a shared lock. This is not evidence
of shared-reader concurrency.

The reviewed raw native writer has a five-second *post-grant write* budget.
Admission contention precedes that budget. No model response stream was found
inside this raw-write boundary: global exclusion covers admission/pipe writing,
not an entire provider response. That distinction does not make its scope cheap.

## Required structural changes

1. **Capture, then release, then decode.** Existing `CertifiedSourceRead` and
   `DeliverySources` own the exact committed source cut and frozen membership.
   Capture bounded original bytes/pointers under the existing barrier; finish
   decoding and consumer work after custody closes. History, addressed pages,
   keyed lookup, context, counts and selected batches must use the same owners.
2. **Await resource operations without exporting resources.** Existing
   `Coordination.run_async` opens, uses and closes its SQLite connection in the
   resource worker, returning detached results. Cancellation joins retirement.
   Every async consumer must use that boundary; moving a connection between
   threads or adding a new executor/store duplicates custody.
3. **Separate send admission from response publication.** A native prompt send
   borrows the global response-publication boundary today. Arendt must trace
   stop, rename, maintenance, claim revocation, reservation and recovery before
   narrowing the existing admission family to the exact owner's custody. Durable
   UNKNOWN remains committed before any prompt byte; cancellation cannot abandon
   a still-writing descriptor. Global serialization should cover the shared
   commit, not unrelated sessions' pipe readiness.
4. **Keep expensive pure presentation work in the existing process renderer.**
   Heisenberg is moving the entire sidebar preparation family from GIL-sharing
   `ThreadWork` to original `RenderTask` / `RendererWork`. Markdown parsing already
   uses process tasks; native DOM mount/style/layout/compositor still needs its
   own source-level reduction. Adding `await` does not parallelize that CPU work.

Do not blindly switch bus readers to LOCK_SH: the current verification hook can
repair/update the durable checkpoint. Certification/recovery mutations require
exclusive ownership. Narrow its lifetime and distinguish an already-certified
immutable capture from a write, using the existing source owner.

Do not blindly enable WAL: both coordination and compaction admission currently
rely on rollback-mode exclusion, and the journal reader explicitly requires that
format. A journal-mode change alone neither fixes long write scopes nor preserves
those contracts. The current pass must reduce the scopes first; any subsequent
mode change belongs to the same authoritative lifecycle design and all consumers.

## Work already present versus outstanding

Core #508 is installed: the committed high-water comes from the existing
certificate rather than an entire decoded log scan. It is a useful partial fix.

#509 source reviewed at `9d557bf4`: bounded conversation/addressed captures,
full-history/context snapshot decoding outside custody, shared sync/async physical
acquisition algorithm and worker-owned native cursor operations. #512 is extending
that same resource boundary into batch selection and acceptance. These changes
are not installed and do not yet prove native admission has narrower exclusion.

The raw admission scope above was sent directly to Arendt as a concrete remaining
structural finding. Async lock acquisition alone cannot solve that serialization.

No new tests were run to discover these findings. After the coherent source change,
validate one actual busy multi-owner channel journey through the installed Toad
entrypoint, correlating admission/lock hold intervals and provider starts with UI
paint/input timing. Report remaining serialization separately from owner-local
turn ordering. Never infer root-wide concurrency from a small isolated turn.

Source inventory and 74 resource scopes across the 19 inspected path modules:
`evidence/installed-concurrency-architecture-20261002/source-scope-census.json`.
The AST inventory records syntax and source hashes, not measured lock durations
or a claim to enumerate every resource in the repository.

## PR #60 AST precedent and batch closure

Read OpenHCS PR #60's actual merge b9cf53e1252f108a61a96d25d85acbc45b8ace3f
and its source commit5e8812ee83d0dc8714392445bad3e32fc47a1755,
`tests/unit/test_cellprofiler_static_deletion_gates.py`. It enumerates the declared
source roots, parses modules once, resolves syntactic dotted references and
class members/annotations, checks unique nominal definitions, deleted imports/
facades and base-owned orchestration. Its inventory derives deleted symbols
from the refactor plan rather than overlooking unnamed consumers. This is a
source-reading precedent; its assertions do not establish dynamic behavior.

Later configuration AST examples cc11e341/c8abd9ed are NOT ancestors of PR60's
merge. They were inspected but are not represented as PR60's changes. The entire
5e8812ee commit changes671files,108307added/41257deleted including nonproduction
content; those figures do not quantify the AST tools' effects or this comms work.

AST mapping and batch migration were sent to all seven existing agents, and the
standing rule is added to AGENTS.md and .pi/APPEND_SYSTEM.md in integrationPR432.
Source shape is a lead: aliases, inherited behavior, reflective references and
external boundaries still require semantic reading. A parser failure is a coverage
gap, never a clean result. Use the existing NRA/refactor-audit tools rather than
creating independent scanners per agent.

The owner's performance target is144Hz (nominal6.944ms/frame), configurable and
overridable at the owning configuration boundary. Heisenberg305/Text22 owns the
whole frame/input path; useful measured checkpoints ship while full target work
continues. Report actual frame/input tails and public busy workload alongside CPU,
not only a quiet private capture. The current installed build does not meet that
target, and no new target attainment is claimed by this source audit.
