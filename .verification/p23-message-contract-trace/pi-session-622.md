# Apply-ready Pi child/session cut

Input: Core `622c26d425e2482d3b430e4c747ebcdeb83a5e31`.
Plan: `pi-session-622.plan.json` (two stages, seven native operations).
Native receipt: `pi-session-622.simulation.json`; author output `pi-session-622-v2.log`; exit in `pi-session-622-v2.exit`.
Author: `pi_session_cut.py`.

**Native simulation is clean. This is ready for friction to compose/apply and checkpoint.** No product edits or cold/runtime test were run by this artifact author. Native input contains all 1,526 Python files under the pinned Core src/tests/tools; friction retains combined owned-Toad integration.

1. Four inspection annotations in `execution/native_child.py` use the existing original nominal parent `NativeSessionRetention[PiSessionChild, NativeChildState, NativeSessionIdentity]` instead of the entire registered `PiNativeBackend`. Their bodies need the parent's `lock` and `child_state`. The original `PersistentPiSession` already inherits this parent; `PiNativeBackend` inherits it transitively. Remove the now-unused guarded concrete-backend import and import the existing low parent.
2. Native `extract_symbols_to_new_module` moves the original concrete `PersistentPiSession` from `pi_native_backend.py` to `execution/persistent_pi_session.py`, rewriting original consumers. No replacement record, forwarding layer, member promotion, vocabulary move or constructor-proof bypass is added.

The child state family, session identity's concrete persisted fields, child creation/reuse/retirement bodies and lock/acquisition order remain unchanged. This generic annotation describes a live provider; it establishes no generic wire-decoding support.

Original consumers include `coordinated_runtime.py`, `native_execution.py` (its original default_factory), `execution/native_session_prepare.py`, `execution/native_session_reopen.py`, and the native fixture/session/compaction tests. Native extraction owns their import rewrites; no compatibility re-export is proposed.

This removes the child-state family's annotation edge to the complete registered backend and takes concrete retention consumers below that backend. No SCC reduction is claimed without a resulting-map receipt. Remaining guarded/local imports in the relocated retention code and the backend are unfinished work, not a condition for applying this cut. Compose these original paths through friction's package_move; normal native reproof handles newer input.

The first author run failed on an exact patch matching four signatures; it is retained as `pi-session-622.log`/`.exit`. Version 2 selects the four actual methods individually and succeeds. The prefix-only plan and receipt remain available, but the complete two-stage cut succeeds and is the handoff.
