# F2: Session coverage

Arendt owns F2 from Tristan's original fold-in-2026-10-04.zip. F0 predecessor
Core620 is open; this source work does not wait for human repository settings.
Reuse the existing checkout. PR618 stays frozen at486bf04a.

## Existing owners and the change

NativeSessionIdentity holds session file/id. NativeWitness adds the prepared
revision, leaf and first-kept entry. NativeCommitPosition holds the returned
committed entry/revision/leaf. FileRevision/FileIdentity own filesystem values.
NativeEvidenceRead owns the descriptor, original bytes and ancestry lookup.
NativeForkCreation owns the returned SDK parent/child relationship and exact
prefix digest/count. CompactionOperation owns committed journal linkage.

Move the repeated session/held-file/position comparisons out of coverage callers
into shared behavior on these existing owners. Read all position consumers before
choosing the final inherited contract; do not introduce a parallel wrapper holding
copies of the witness/outcome/fork fields. Keep before-write and committed positions
distinct. A matching session is not sufficient proof of an unchanged prefix.

Compaction payload/metadata digests, marker identity, kept-cut ordering and fork
prefix bytes remain distinct checks with their existing owners. The shared coverage
decision must consume them without weakening refusal or rewriting original records.
No input admission, enrollment, recovery or replay permission follows from coverage.

## Family and callers

The five covered_prefix definitions are CompactionOperation, NativeForkCreation,
NativeEntry, NativeEvidenceRead and ManagedCompactionEntry. recorded_prefix combines
original fork records. NativeEntry's default is empty coverage; ManagedCompactionEntry
delegates to the original journal operation; NativeEvidenceRead selects actual
ancestry. These are distinct behaviors, not five copies of the same field check.
Delete the duplicated identity comparisons in the operation/fork paths and review
recorded_source_prefix's admitted input anchors through the same session contract.

Consumers include PrivateInputs.fork creation publication, continued private-session
coverage, historical native context/input proof readers, and retained measurement
checkpoint/SDK-child reads. Native preparation and the SDK fork helper produce the
original witnesses; journal rows preserve them. No new persisted position/schema,
legacy reader or reset is assumed. Any actual format change needs its full producer,
reader and stopped installation closure before release.

Initial AST uses existing refactor-audit Package across src/tests/tools:311/364/53
modules, zero omissions. before.json includes inherited declarations, imports and
coverage calls. It is lexical evidence; dynamic dispatch is resolved by source
reading, not a claim from zero matches. Shared files will be coordinated directly;
Mendel619 session identity publication and Sch trust/build remain disjoint.

Source reasoning, complete owner/caller implementation and deletion first. Batch
focused checks and the actual configured saved-compaction/continuation journey last,
with originals/UNKNOWN preserved and no replay. No environment, provider variant,
package/native mutation or thin540 loan is implied by the initial plan.

## Working implementation

NativeSessionIdentity.covers is the single physical coverage relation: original
Path and session header, plus each required original FileRevision's inode and
minimum prefix length. NativeWitness and NativeForkCreation inherit it. The
required revision argument prevents an identity-only call from claiming coverage.
The prepared and committed revisions remain separate original observations.
A prepared prefix must also fit in the held file; later appends remain legitimate.

NativeWitness.require_committed_cut owns the prepared parent/first-kept relation;
NativeCommitPosition.require_entry owns the returned entry. NativeIntent owns
journal-file linkage, exact marker and payload/metadata corroboration. These are
distinct facts, not optional terms selected by coverage callers. The operation
now calls those owners and retains branch ordering. NativeForkCreation owns its
SDK parent relationship/count/digest and delegates physical coverage. All original
field declarations, SQLite columns and native wire names remain unchanged.

NativeEvidenceRead.retained_task_facts uses the inherited session relation between
its existing exact preparation checks. recorded_source_prefix delegates file
membership to the existing acquired reader; original admitted-anchor ancestry is
unchanged. No coverage call settles UNKNOWN or grants admission/replay. The reader,
entry and managed-entry covered_prefix methods remain orchestration/default hooks.

The exact F4 SQL handoff is integrated: SelectedSummaryAttempt.transition now
passes FieldCodec.encode(type(self.state)) at its unchanged json_extract(kind) CAS.
The existing codec encodes a DeclaredFamily CLASS as its scalar declared name;
encoding the INSTANCE would instead produce a tagged dict. JsonStorage still
encodes SET values; TypedTable still passes WHERE parameters through. No predicate,
transition, schema or coverage change is implied by this one-line projection.
Mendel/parent granted this exact edit from ca1bf0e0; no other F4 changes copied.

The existing configured fixture's VCS-only origin lookup could not read normal
file-wheel metadata. It now borrows the existing InstalledSource declaration for
that origin, which binds actual module/location/directURL to the reviewed artifact.
Its complete Git-to-imported asset verification remains required. No synthetic VCS
metadata, source overlay, new proof class or relaxed equality. Existing VCS callers
retain their original origin path. The F2 driver reuses run/configured_saved_agent
for one original fork, summary and distinct input; after terminal it observes the
original creation/commit through the unchanged acquired reader/journal owners.

Source checkpoint 97785da5: hosted Debt ratchet PASS (37178636773). After.json
uses the existing parser over src/tests/tools (311/364/53, zero omissions). One
session coverage declaration; unrelated bus-page/receipt covers methods are
separate meanings. covered_prefix retains only branch ordering, selection and
absent-operation orchestration comparisons, with no direct identity comparisons.

Final source sanity: 10 compaction-boundary cases PASS, including original
committed/fork coverage, append and corruption controls. The combined batch has
18 PASS/17 FAIL: the other file's invalid-ID wording/removed session_dir callers
are unchanged baseline source, not corrected or hidden here. Initial interpreters
lacked test dependencies; no packages were installed. Existing534 supplied read-only
dependencies and system pytest supplied the runner, with bytecode disabled. This
is a source check, not installed production acceptance.

## Installed qualification complete

Frozen source600c6fb0 includes the exact SQL class projection and original filewheel
boundary. Hosted Debt PASS37179917875. Bohr granted exclusive package/execution use
of thin540; Sch granted unchanged native086 read/execution. One normal Core wheel
SHA5b4bdc1daed96ccb98d92cddbb369f3e750cb2e981eb219ee85b5cacd000208a
was installed. All339 original Git assets match, all60 dependency metadata files
are unchanged, and immutable086 full trust matches its declared manifest/tree.
No environment/dependency/native artifact build or public package mutation.

The ONE configured Sol/HIGH saved-fork compaction/distinct-input journey PASS
128.990s through existing configured_saved_agent/run. Actual source42,662,475B,
SHA1bdd97fd57551edb2986a3289b33bc781cde45b98b4e1e591f79e7804ced7258.
Manual commit36a1df1ab1f044dab354695151db024b, one actual peer publication during
summary, then one distinct fresh input/answer. No original input before that prompt.
Original source/proof unchanged; native children closed; no public input/restart.
Completed original journal coverage corroborates all9,644 inherited entries.
Actual driver exit0; raw receipts retained in.artifacts/session-coverage623/configured01.
Sanitized exact receipt/SHA is evidence/session-coverage-position-20261004/installed-configured.json.

This qualifies the changed installed saved-source compaction/continuation coverage
family. It is not physical UI, comparative study, HTTP, recall/cost, overall latency
or full S4 acceptance. Existing source negatives remain visible; no repeat was run.
