# Native proof journal decoder closure — PR283

Based on current main33fd5ac9 (including merged277/279/281/282). Wegener owns
native reader cap/run_native_pi_turn; this diff touches only proof declarations,
the retained-journal decode call and related tests. Preserves NativePiPromptRejected
and canonical config-directory preparation. Parent owns integration/deployment.

## Durable classification and unchanged contract

Native `.input-proof` is durable evidence paired with native session history, not
an index to reset. Preserve both in place. No schema, producer, stored key/value,
format, migration tool or runtime reset change. Pinned native AgentSession writes
schema1/type=context_committed; its journal producer is unchanged.

NativeContextJournal declares this current envelope on NativeContextRecord's
inherited five context fields. Located NativeContextProof inherits the same facts
and keeps its actual session path. FieldCodec decodes once; the typed record's at()
locates evidence without granting acceptance/replay. The old from_journal mapper,
hand-written key-set switch, per-field codec loop/private _types access and fields
import are deleted. Existing corroboration, duplicate JSON keys, private file and
revision checks, tracked-entry binding, generation/digest agreement remain.
No second registry, codec, fact-field roster, historical reader or nullable proof
was added. Existing located-proof callers keep using their current typed facts.

## Actual evidence

Candidate installed as a fresh noneditable wheel in owned .artifacts/paired-installed.
No source override for installed tests/native/history checks; tests themselves are
loaded from this branch. Pinned verified native package5fdef596596173bd unchanged.

- installed-boundary.log:12 passed0.15s. Real filesystem corruption/provenance,
  current envelope strict rejection (schema bool/missing/unsupported, unknown key,
  wrong type/generation/input/digest), retained generation and file mutation.
- installed-native.log:2 passed8.55s. Actual installed run_native_pi_turn ->
  pinned native Pi -> loopback provider; normal one-input path, then a second
  fresh child reopening saved native history with one distinct new input.
  Exactly one provider call per new input. First generation's original proof
  remains equal/verifiable after subsequent generations. No replay/retry.
  AC_NATIVE_COPIED_PACKAGE=<owned pinned package> <installed python> -m pytest
  -o addopts='' tests/test_native_pi.py -k
  'copied_cli_private_policy and (restarted or stop)'.
- retained-proof-receipt.json / retained-proof.log: actual existing NRA session
  copied read-only into owned persistent artifact directory:39417474-byte session,
  5185701-byte proof journal,9064 native entries and148 retained contexts verified.
  Installed reader left copied history unchanged; source revisions stable during
  copy. Zero provider requests/replay, disposable44MB copy removed afterward.
  Reusable operator retained_proof_acceptance.py takes source path, owned artifact
  directory and receipt path. Original source files were never opened for writing.
- installed-current-callers.log:29 passed1 skipped8.94s. Prompt-binding consumer,
  native streaming guard and S10 deletion guards. Existing S10 guard also prevents
  the deleted from_journal mechanism from returning.
- Changed source/tests/operator lint passed. Source +34/-36; tests +34/-0.
  New test protects the unchanged external native envelope; native reopen test
  strengthens retained-proof behavior. No deleted-contract test was ported.

## Honest remaining test limitation

Broader installed-callers-final.log:43 passed1 skipped9 failed. All failures enter
reserve_selected_summary with an obsolete SelectedSource fixture missing its
current family discriminator, before the proof decoder runs. The representative
failure reproduces unchanged on actual current-main source33fd5ac9 in
main-baseline-fixture-failure.log (1 failed); it is not counted green. Existing
Wegener/parent native fixture closure owns that seam; this PR does not restore an
old reader or duplicate their selected-source edits. Initial installed-callers.log
selected a nonexistent historical-input test file and collected no tests; retained
for honesty, not counted as acceptance. No whole-suite/CI claim.

Universal plan size/benchmark qualifications remain as in merged audit281.
No live route/launcher/history/settings writes, paid provider probe or auto resend.
All owned new baseline/test/history copies are cleaned after retaining receipts;
installed candidate/native package stay for integration reproduction.
