# Canonical fork creation closes inherited-context coverage

## Missing fact and existing owners

The old SDK helper returned only child NativeSessionIdentity. No retained
creation witness authenticated its initial inherited prefix. Parent headers,
routing names, later context digests and original source-side commit markers
cannot recreate that missing fact. Original 201 remains unqualified; its receipt
and UNKNOWN input are not rewritten or re-enrolled.

SessionManager.forkFrom already owns the source and destination writer locks,
O_EXCL creation, copy, file fsync and parent fsync. Its creation hook now emits
the source identity/revision and newly written child identity/revision, exact
prefix digest and entry count within that same lifetime. The digest and count
advance from the bytes actually written, without another read or lock. No
compaction firstKept value is invented, and no provider/model policy changes.

NativeForkCreation is a genuinely missing creation fact, not a replacement
session family. It extends existing NativeSessionIdentity, SessionJournalHistory
and TypedTable. The same typed result is retained once in the existing journal;
there is no receipt copy, new store, secondary registry or process-local seen
list. FieldCodec owns boundary conversion. The helper's family tag derives from
the declaration rather than a competing native codec. Existing FileRevision and
TextDigest own its filesystem observations and content identity.

PrivateInputs.fork owns invoking the canonical creation once, verifying the
unchanged initial resource and publishing that record before exposing it to
input. It accepts a source request, never an old receipt or observed child path.
ThreadManagement uses it before launch. Every actual installed fork journey now
uses that owner; no helper-only continuation caller remains.

SessionJournalHistory.exists derives membership from TypedTable declarations.
Both pristine-creation and continued-coverage decisions share it, deleting the
independent enrollment/raw presence combination. Empty O_EXCL enrollment retains
its original authority and fields; it is not repurposed as fork enrollment.

NativeEvidenceRead borrows the stored creation through the same journal
transaction. NativeForkCreation verifies the current original path/header/inode
and the exact initial byte prefix using PrivateEvidenceRead.verify_snapshot.
Inherited markers are already within that authenticated prefix and therefore
cannot be interpreted as commits made by the child. Later child commits remain
subject to original CompactionOperation corroboration. Every private raw UNKNOWN
and unresolved input still requires its own original proof and disposition.

## Format and delivery boundary

The existing compaction journal gains one declared table. Old journal readers
are not supported by a fallback and existing journals are not automatically
upgraded. Mendel owns the stopped carry: add the new table EMPTY from the target
declaration. No retrospective creation record, old-attempt grant, original
source JSON transformation, native context-proof change or reset is allowed.

The native package must include the new fork hook before this helper can return
a creation record. Old packages cannot qualify by a substitute decoder. Sch owns
the one coherent native artifact after source closure. Current public packages
and original 102/201 files remain untouched.

This checkpoint is source, not installed acceptance. The complete affected
sanity batch and one authorized configured saved-fork optional-compaction/next
answer journey follow the coherent implementation. No original input replay,
paid call, original-source mutation or new fixture was made while reasoning and
implementing this contract.
