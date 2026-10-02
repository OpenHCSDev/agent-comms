# Scoped native input evidence — ready checkpoint

PR534 changes the existing NativeEvidenceScope acquisition to NativeInputEvidenceRead.
Production source checkpoint: `d5bef7397877ec56d000a1941c225835b1678b34`.
Production: one file, four lines deleted/four added. Its complete production consumers
are HistoricalNativeInput, SourceCoverage and NativeSourceCursor. Full history rendering
and retained task readers retain their separate existing full-history acquisition.
No new owner, cache, status, schema, native patch or provider call.

## Actual installed read

A normal wheel installed with declared dependencies in `.artifacts/runtime-scoped-input534`
matched all 311 production Python files. Existing SourceCoverage.prefix/evidence/last_proof
read the original retained native/ACP fixture `/home/ts/wt/p530/wire` (42,932,701-byte
native journal), without launching a child or submitting input.

- Before: 2.883174014 seconds; 9,757 full-history entries decoded.
- After: 0.787411265 seconds; 205 native input records decoded.
- Coverage, both original native proofs and last input identity identical.
- All protected original file hashes identical before/after; acquired resources closed.
- Five existing custody controls passed in 0.29 seconds: descriptor switching/closure,
  refused evidence cannot reopen, and acquired-resource cleanup preserves original errors.

Exact installed comparison, source file hashes, requirements and control output are adjacent.
Before used actual default326 Python; after is a fresh normal candidate installation.
Base is main dd79dabc, including reviewed532 native0b306 declarations; no native assets
were changed here. The default385 nativead533 cohort remains protected. Python source
between original530/default385 and this base was identical before the four-line change.
An evidence exporter first rejected an external marker key and a PosixPath serialization;
only exporter projection was corrected. Those reads launched no children or inputs and
changed no source/store contract.

## Actual385 limit and next action

The adjacent original-input/lease timeline joins 20 inputs, 10 request streams and ten
stable saved journals to the original message. FULL native requests took 3.638–7.684s.
Native finish to the existing acquisition marker took 2.234–14.243s; marker to canonical
publication receipt took 1.432–11.663s. These encompass multiple owners; they do not
attribute that elapsed time to a lock, provider or single function. SQL transition and
intent timestamps are not commit-completion timestamps. Most event receipt delays were
milliseconds; helper2 had a late 8–9s observation interval.

This checkpoint removes proven unnecessary full-history decoding from input proof work.
The measured saved-state read is not a new native/ACP/UI journey and does not prove the
public 38–49s reply latency fixed. No additional original-message probe or paid turn was
sent. Parent owns paired installation and configured-channel acceptance; Arendt owns
native cold restore/get_state. Preserve all original385 and private530 proofs/UNKNOWN.

Before/after declaration and consumer output: `scope-before-after-ast.json`.
Existing NRA parsed all 311 production modules at each exact revision, zero failures.
All three scope import consumers are unchanged; only their existing scope factory
and precise resource annotations now select the input-proof decoder. Dynamic dispatch
is not established by this AST inventory; semantics and the installed read establish
the affected factory path.
