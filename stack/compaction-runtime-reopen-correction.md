# PR95 idle native manager retirement and strict fresh reopen — bounded slice

A persistent Pi RPC child holds its injected `SessionManager` reference and
cannot safely be rebound by assigning a public manager field after another
process rewrites its saved session. `PersistentPiSession.discard_for_external_write`
now terminates the idle child under its borrow lock, preserves the expected
canonical file/session ID, and marks the session for strict validation. It does
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
journal-backed adaptive summarization and its caller of
`discard_for_external_write` are **not yet wired**, and that manual refusal is
not a complete replacement. No installed or live session was touched.

Provider-free evidence: disposable pinned-package strict valid/reopen byte
identity; ambient preload cannot execute; five invalid cases (torn tail,
legacy header, ancestry, missing file, alias) do not repair; real backend persistent fake RPC
retires old PID, validates preflight, verifies fresh `get_state` before a
*distinct* prompt, retains only after settlement; invalid JSONL stops before
RPC spawn and never sends the refused input; canonical legacy `/compact` route
fails closed. Logs `/var/tmp/pr95-reopen-*.log`. The ACP final send gate in
`compaction_send_admission.py` independently refuses unresolved commit intents
and UNKNOWN before input bind, including correction and direct paths.

Remaining: integrate an owner-only adaptive preparation/summarization/commit
call site, execute this retirement **before** its native external write, and
prove real provider-free end-to-end multi-round recovery. Exact local ACP
metadata projection is separately wired in `compaction_publication.py`; it
remains distinct from a bus broadcast or inferred recipient. Neither runtime activation nor
merge clearance follows from this bounded slice.
