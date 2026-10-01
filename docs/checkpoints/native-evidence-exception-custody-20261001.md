# Native evidence exception custody — Ready, 2026-10-01

Owner Arendt; PR498 receives main f16b2081 normally, including merged494/497.
Functional source freeze: `59649992a2c4324cd8bf1512380fed7b38266885`. Parent owns merge and activation.
Production diff against receiving main: **8 files, 180 added /119 deleted**.

## Source ownership and deletion

PrivateEvidenceRead and NativeContextJournal translate only their own acquisition,
file I/O, decode and observation failures. Existing ExitStack custody closes
resources while preserving consumer exception identity. NativeTranscript.tail,
PrivateEvidenceRead.rows and BusPageIndex.iterate likewise put translation around
iterator advancement/decode, outside yielded consumer execution. Original prefix,
inode, permission, schema, generation and mutation checks remain intact.

The source census distinguishes legitimate operation owners: GoalHistory owns its
private write/commit/fsync transaction; backend.run owns turn completion/failure.
CompactionJournal, CoordinationSession and NativeCustody rollback/cleanup catches
rethrow unchanged. No blanket removal of operation-level failure ownership.

StoreLockContention owns the existing **two-second physical POSIX lock wait
resource** at _store_lock. One borrowed resource passes through original wire →
bus → registry → coordinator SQL order. NativeSourceCursor advances once with one
NativeEvidenceScope. The old whole-observation retry loop is deleted: decoding and
consumer work cannot spend the wait resource. Only refused physical flock waits;
source operations and provider inputs are never retried. SourceCoverage's original
0.25s certified-acquisition guard excludes measured physical waiting only; no new
addressed-page wall policy. _require_source keeps its original read signature.
Existing certification and current root/process/admission/generation/prompt/SQL
and monotonic-cursor fences remain with their original owners.

This resource contains no session/input/cursor/proof/replay authority. Windows
locking behavior remains unchanged; acceptance exercises private POSIX execution.
Native, wire and storage formats are unchanged.

## Final installed qualification

[Ready receipt](../../evidence/native-evidence-exception-custody-20261001/READY.json)
and [provenance](../../evidence/native-evidence-exception-custody-20261001/installed-provenance.json)
record all eight installed modules byte-equal to source, a normal wheel installation
and unchanged reviewed native53b8. No editable import fallback.

- Final focused resource/index controls: **25 passed in 1.18s**.
- Ordinary private restart/automatic drain gate02: three real native owners, three
  input-backed cursors, 21 ACP notifications, zero diagnostics; 22.73s, retired.
- Representative retained gate12: **24.653549775s, all three input-backed cursors
  proven, three localhost POSTs, 18 ACP notifications, zero diagnostics**; retired.
  Three declared private copies of the original **42,924,939-byte** journal and
  original input-proof ran through actual CommsAgent/TurnRunner/SelectedExecution,
  native children, coverage, cursor and ACP publication. Original hashes unchanged.
  Real wire contention lasted 1.708709740s with all three original source FDs open
  after native IGNORE. The stack records physical StoreLockContention acquisition.
  Original single cursor calls took 2732.30/2469.92/2617.69ms, preserving the lock
  allowance despite source decoding. No state/protocol/UI implementation mocks.

Gate12 command: `.observations/runtime/bin/python tests/shared_bus_restart_native.py
--stage /home/ts/wt/ac498-12 --owners 3 --history 120 --collective
--saved-source /home/ts/.cache/agent-scratch/parent-authored474-stopped-saved-startup-20261001/session.jsonl
--cursor-contention --package /home/ts/wt/comms-combined474-global491-native-20261001/stack/.pi-native-53b8c413d90aa6b1/node_modules/@earendil-works/pi-coding-agent`.

## Explicit limits and protected originals

This qualifies resource custody and contention with **declared private retained
sources**, not ordinary ACP continuation of public saved sessions. Private drain
currently omits captured saved-file selection; launch/attestation/admission/proof/
recovery independently assume private location. Full continuation through existing
NativeCustody, NativeSessionIdentity, attach_session and raw coverage remains
**Arendt PR489**, coordinated with Einstein and Schrodinger. No weak factory patch.

Gate01's original terminal assertion failure remains, alongside read-only corrected
input-backed qualification. Retained08's two-of-three result, retained10's measured
2.67–3.37s decode/deadline failure and retained11's unused-argument TypeError remain
failures in committed evidence. Final12 closes this resource scope. Earlier broader
fixture controls yielded 46 passes/11 obsolete-fixture failures; no green full-suite
claim. No further tests or CI were run after the accepted final gate.

Schrodinger's read-only incident classification found all six original triages
terminal IGNORE with matching original proof and empty current pointers; no recovery
mutation/replay required. Historical UNKNOWN dispositions remain untouched.
Raw ac498-01..12 roots, original saved journal/proof, failed receipts and native
journals remain protected. Owned .observations/runtime (~83MiB) and profiling files
are retained. Git contains sanitized receipts/stacks/provenance only, no native
bodies, credentials or registry preimages. Zero public mutations or paid calls.
