# PR95 idle native manager retirement and strict fresh reopen — historical bounded slice

The exact `12050b1` implementation had an independently demonstrated
cancellation/reap and native-launcher-alias gap. Preserve that NON-CLEAN review;
see `compaction-runtime-review-corrections.md` for the successor's repair and
provider-free fault evidence. This document's original happy-path evidence is
not a clearance of the old bytes.

A persistent Pi RPC child holds its injected `SessionManager` reference and
cannot safely be rebound by assigning a public manager field after another
process rewrites its saved session. `PersistentPiSession.discard_for_external_write`
attempted to terminate the idle child under its borrow lock, preserve the
expected canonical file/session ID, and mark strict validation; the old
ordering was cancellable before the marker and is corrected separately. It does
not transparently retry the failed or uncertain input.

Before the next input can launch a fresh Pi child, backend transport invokes
`validate_native_reopen`: derive the package from the canonical launcher and
compare its manifest with the Python wheel/source resource, verify the entire
immutable tree, strip inherited Node preloads/search/cache, preload the copied
import fence, then call the **read-only** pinned `loadEntriesFromFile`. It
requires an independently regular bounded canonical session, strict v3 JSONL,
unique valid entry ancestry and unchanged disk revision across validation.
It never calls `SessionManager.open` for preflight because that could migrate a
legacy file. The fresh RPC `get_state` session ID and file must also match the
prevalidated evidence **before** a prompt send. The marker only clears after a
fully settled validated turn is retained, not on startup or a refused send.
Malformed/torn/legacy/wrong-ID/alias sessions fail before provider input and
are not repaired or replayed. The save lock spans validation and native launch.

The earlier manual `/compact` bridge uses a separately installed legacy Pi
package and lets that Pi child commit directly. The canonical `pi-native`
launcher therefore refuses this legacy route rather than creating an unjournaled
alternate writer; other historical/manual fixtures remain unchanged. Owner
journal-backed production adaptive summarization and its ACP call site are
**not yet wired**, and that manual refusal is not a complete replacement. No
installed or live session was touched.

Provider-free evidence: disposable pinned-package strict valid/reopen byte
identity; ambient preload cannot execute; five invalid cases (torn tail,
legacy header, ancestry, missing file, alias) do not repair; real backend persistent fake RPC
retires old PID, validates preflight, verifies fresh `get_state` before a
*distinct* prompt, retains only after settlement; invalid JSONL stops before
RPC spawn and never sends the refused input; canonical legacy `/compact` route
fails closed. Logs `/var/tmp/pr95-reopen-*.log`. The ACP final send gate in
`compaction_send_admission.py` independently refuses unresolved commit intents
and UNKNOWN before input bind, including correction and direct paths.

`owner_compaction_prepare.py` now reads the strict v3 file through the exact
pinned native loader **in memory** (no persistent manager/implicit repair), uses
Pi's `prepareCompaction` cut point and declared default recent window, and
emits only witness/token metadata. `OwnerCompactionCommit.prepare_source` then
captures canonical owner + bus + input revisions **before** summary generation;
the existing writer rechecks that exact source and its native revision at
commit. Tests use a bounded recent-window override and provider-free synthetic
summaries to exercise a correction that invalidates the source, and three
sequential native commits with exact distinct IDs. The internal
`compact_owner_once` sequencing helper also uses an injected provider-free
summary callback to prove retirement **before** the native writer, including a
late correction that refuses mutation and leaves reopen validation required.
It has no production ACP caller or model strategy yet. The later cancellation
correction joins the exact in-flight native commit worker before the cancelled
owner turn can release its async lock; a real pinned-writer test confirms
persistent intent during the wait, one committed exact ID afterward, and strict
invalid-disk denial before the next fake RPC launch. This demonstrates
structural source/admission recovery, **not** summary quality, provider routing, cache
behavior, or semantic memory retention. There is no general OS subprocess
sandbox; pinned-package import/process checks are scoped to audited native
package-manager and helper entrypoints.

Remaining: integrate a bounded owner-only provider summarizer and runtime/ACP
trigger/call site, then prove multi-round provider-free end-to-end behavior
through that actual call site. Exact
local ACP metadata projection is separately wired in
`compaction_publication.py`; it is not a bus broadcast or inferred recipient.
Neither runtime activation nor merge clearance follows from this bounded slice.
