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
package/native mutation or thin540 loan is implied. ## Working implementation

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

SelectedSummaryAttempt.transition is untouched. Its declared_name WHERE parameter
is an SQL scalar projection of the original member; JsonStorage encodes SET values,
while TypedTable leaves WHERE parameters unchanged. A tagged FieldCodec dict is
not a substitute for json_extract(state, '$.kind')'s scalar. Shared-site advice was
sent directly to Mendel; F4 does not need an F2 codec/schema change.

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

Matching installed configured saved-compaction qualification remains pending.
No original/provider/study run was repeated. No new environment, native artifact,
package mutation or public effect. Failing ratchets do not permit merge.
