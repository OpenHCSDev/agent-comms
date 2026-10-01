# Acquired evidence resource closure within #489

Existing owners were read before editing: NativeEntry.open_evidence acquires the
strict PrivateEvidenceRead; NativeEvidenceRead owns decoded bytes and prefix
observation; NativeEvidenceScope owns bounded descriptor lifetime. NativeContextProof,
PromptBinding, SourceCoverage and CursorOwner own different semantic decisions.
Neither a reader nor its dictionary of acquired descriptors establishes those facts.
The normally integrated #493 checkpoint retains that distinction.

## Acquisition behavior belongs to its existing resource

NativeEvidenceRead.borrow owns optional borrowed/acquired reader selection, strict
path binding, and failure/cancellation cleanup. NativeEvidenceScope.borrow owns
the corresponding operation-scope lifetime. These are methods on existing resource
owners, not new families, semantic states, evidence stores or nullable authorities.
Successful borrowing preserves the original caller's acquired lifetime; refusal
retires the same bytes/descriptor. Every observe still validates ancestors, file
identity and the immutable consumed prefix before decoding new bytes.

Delete repeated optional-resource recursion from NativeContextProof.read_evidence,
read_tracked_input_digest, historical input reads and SourceCoverage.read/evidence/
prefix. NativeContextProof still reads the original context journal on every
corroboration; no stored reader proof can grant admission. `_verify_context` compares
the original emitted record with that proof in the borrowed resource lifetime,
removing its independent nullable cleanup branch.

Continued-private-session verification previously decoded the session, closed it,
and reopened/decoded it for each recorded input. It now holds one original acquired
reader through the complete original-user/history check. Original input disposition,
raw-ID membership, context columns and final reserved SessionRevision check remain
required; old UNKNOWN is never promoted by observed bytes.

Released-native-failure recovery previously opened the same session separately for
prompt digest, context proof and terminal history. It now borrows one reader for
those checks, retains the original owner-loss exclusion through terminal settlement,
and closes the resource on exit/refusal. Original frozen publication refusal,
input/binding identity, complete failed terminal, subprocess death and fenced
ReplayAssessments join remain determining authorities.

## All receiving consumers and boundary closure

- Private send stage already acquires one original reader for prompt/context.
- TrackedTurnSession already holds a reader through its existing ExitStack and
  rebinding seam; failed corroboration closes it. No source/header/proof mirror is
  added here.
- NativeContextProof.read_evidence/corroborates_input, tracked digest and emitted
  context verification consume the same resource borrow method.
- Historical proof reads and SourceCoverage read/evidence/prefix reuse the original
  operation scope; native_inputs/last_proof delegate to that same historical owner.
- NativeSourceCursor advance/read retains its existing whole-prefix scopes and
  rereads current CursorOwner/SQL proof. No cursor eligibility or position changes.
- Continued-private-session and RecoveryMonitor consume their explicitly acquired
  operation resource through the original context/digest readers.

Original proof/coverage/SQL/owner/session decisions remain unchanged, including
different input versus per-source assignment identities. Kepler #490 owns membership
producer changes; its schema5 preservation belongs to Singer #495. This checkpoint
changes resource behavior only, and does not reintroduce removed anchor fields or
edit another owner's membership algorithm. Normal integration resolves their
consumer diff without replacing the original source relation.

Patterns: IMPL-12/13 (repeated acquisition/decoding), IDEN-3 (resource absence must
not masquerade as lifecycle state), BOUND-2 (consume original proof owners). A new
corroboration consumer borrows through the existing resource method and reads its
original proof owner; it adds no acquisition recursion or proof cache.

## Evidence and remaining scope

[Authored source search](../../evidence/runtime-lifecycle-semantic-pass-20261001/phase-boundary/core-consumers.json)
records resource declarations and all direct authored consumers; displaced consumer
`if evidence/source_reads is None` recursion is absent. The resource owners retain
their genuine optional borrowing contract. Search results establish locations,
not execution or correctness. No tests/native/provider calls ran during this pass.

Full #489 final request/compaction budget, paused original writer continuation and
canonical terminal/readiness closure remain unfinished. This is a source checkpoint
in the integrated draft; validation and the affected installed journey run LAST.
