## Complete R3 ownership and caller closure

- Move UNKNOWN/STARTED behavior to declaration-owned input records; typed input/cursor documents use existing LockedStore/FieldCodec.
- Delete raw persistence/status/accessor facades and migrate all core/test consumers. Public saved/ACP shapes remain unchanged; process-local queue authority is not reconstructed.
- Preserve compaction CAS while removing discovered input-lock reacquisition deadlock; journal begin consumes the retained typed snapshot.
- Reconcile main197 PiPayload: codec family_discriminator is distinct from native wire_tag. Cache declaration metadata only.

## Evidence

140 consumer cases and146 journal/document cases; final6 native competing-writer and2 actual ACP/native local-fake queue cases;53 final shared-boundary cases,45 focused caller cases and2 native stack cases (935 UNKNOWN inbox and saved-history compaction). Parent independently reports96 shared-boundary,4 Toad pilots and exact saved-data equality (7780 input rows,96 cursors,2580 UNKNOWN). No live writes or paid provider calls. Earlier failed/timed-out attempts retained and distinguished in evidence/r3-input-documents/HANDOFF.md.

Matching integration launcher resolves the initial stack fixture pin mismatch; no native rebuild. Parent integration198/Toad97 owns installed acceptance and activation; no CI wait. Full caller/deletion and lock-order map: evidence/r3-input-documents/HANDOFF.md.
