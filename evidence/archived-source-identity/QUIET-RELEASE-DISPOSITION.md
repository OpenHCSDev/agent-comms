# Parent-owned quiet release disposition

Prepare only; no public mutation occurred. Both source-0njqz198 and
source-a7v0vr4w require two independent transitions: required recorded provenance
in history_sources.json, and the current derived private checkpoint schema/seal.
Parent installs the matching reader, owns quiet admission/all-stop/install, then
executes the existing checkpoint installer and publishes the one-shot manifest.

## Exact schema authority and proof boundary

DeliverySources in private_bus_checkpoint.py196-202 gained sender_lookup and its
(sender_lookup,seq) index in 2a12d65f6 (Core430). The original archives contain the
same seq/message_id/offset/length columns without that derived lookup. The added
lookup derives ONLY from CommittedDelivery.audience.sender_lookup in original
certified wire; it is not today's registry name. S5 275a7847 changes no tables,
columns or indexes; silent context observations participate in the existing
physical byte/digest seal and contribute no public-message sequence/index row.

The checkpoint database is derived, never the original wire/assignment authority.
The source checkpoint's old SQLite bytes remain protected diagnostic evidence.
Do not reset any other SQLite tables/store. Native journals/.input-proof/UNKNOWN,
wire, registry, original frozen audience/claims, aliases, goals, transcript routes,
read ledgers and dispositions remain protected, including retired live fields in
raw historical registries. No history/source collapse, prompt or replay.

The existing FinalSeal binds checkpoint inode/revision and PrefixWitness
(root_id, opened bus device/inode/size/mtime/ctime, through_seq, digest, tail).
WireMetadata carries that seal in checkpoint_version/checkpoint_seal. Its other
fields (root_id, last_seq, admission_after_seq, access, writer and claim protocol
versions) remain identical. PrivateRegistryGuard binds registry bytes to original
root_id/sequence and is untouched for an in-place rebuild. The HistorySource
snapshot revisions fence bus+registry, not the replaceable checkpoint/marker.
No broader whole-bus_meta hash is substituted for this declared seal authority.

## One-use sequence, executed only by parent

1. Under the existing ORIGINAL-SCHEMA writer custody (`with WireLog(bus).locked()`), validate the
   source's original bus+registry revisions against HistorySource. Preserve the
   exact old checkpoint database and bus_meta.json preimages outside both archive
   directories, with original ownership/mode, hashes and manifest/source facts.
   Capture original raw source/proof/native hashes and every determining
   ThreadProvenance field for each of the 104+7 records plus the 9+0 aliases.
2. Read the original typed WireMetadata through WireLog. Clear ONLY
   checkpoint_version/checkpoint_seal, then retire ONLY the derived checkpoint DB
   (preimage retained). Publish that marker through its existing writer. This is
   the declared quiet reset; no live decoder of historical Thread is involved.
3. The new installed child inherits that SAME opened bus lock descriptor through
   the existing retained_index_writer/install_retained_index handoff. Its
   require_retained_writer verifies the original named lock inode and root_id;
   it never reacquires the writer lock. Invoke
   install_private_bus_checkpoint(log, _bus_locked=True). It builds all
   current CheckpointTable declarations off-path, verifies every original wire
   row and frozen audience, and commits the original physical prefix and new DB
   inode into the existing FinalSeal. Do not supply or copy a new source identity.
4. Assert all noncheckpoint marker fields identical, original root_id/last_seq,
   unchanged bus+registry/native/proof hashes/revisions, all 111 source-scoped
   incarnations/tags/worktrees/session paths/titles and nine aliases preserved.
   Use the current certified reader/_saved and normal bounded history/transcript
   reads as final custody checks. A failed/reset-only installation denies reads;
   preserve its receipt/preimages and do not hide failure or restore UNKNOWN.
5. Run tools/cutover/carry_history_provenance.py with explicit original manifest,
   fresh candidate output and receipt; it projects only declaration-owned facts
   from frozen evidence outside src. Parent atomically publishes that candidate
   ONLY after this reader is installed and the quiet gate succeeds. The source
   roots, bus identities, snapshot bus+registry revisions and native paths stay
   unchanged. There is no pre-cutover production reader or schema fallback.
6. Retire the one-shot executable after publication; retain its exact reviewed
   bytes/hash, invocation, preimages and preservation receipt as audit evidence.
   Original archives/proofs are never cleanup targets.

The installed private journey rebuilds ONLY copied checkpoint/registry inode
custody (copied registry bytes remain exact). It demonstrates the reader against
real original histories; it does not establish public runtime readiness before
parent executes this quiet sequence.

Important: the target _store_lock validates the current checkpoint schema before
yielding. Acquiring target WireLog.locked() against either original old checkpoint
would fail before reset. The original-schema writer is required for the existing
handoff; the parent selects its reviewed retained interpreter. A generic new-reader
lock acquisition or raw independent flock is not the release procedure. The exact
invocation/preimage checklist is in PARENT-CARRY-INVOKE.md.
