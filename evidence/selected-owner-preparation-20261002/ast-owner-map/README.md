# AST owner and caller map for #506

Uses `Package.load`/`Repository` from the authoritative refactor-audit archive. OpenHCS PR60's declared-root/member/import/inheritance method was read; its helpers were not copied. This existing scoped adapter adds no product parser, framework or guard.

## Exact snapshots

- Before Core: `a87a706521e36de855ca211436f6a507cfb6b98c`.
- After Core: `34c5a666091ebd6cc8014b9cb507fb1c2f26075a`.
- Toad dependency snapshot for both maps: `21f8651f8d9c62da4b615fa48fb1177d26a84ea0`.
- Whole tracked Core `src/tests/tools` and Toad `src`: 993/994 parsed Python files, zero parse failures.
- Relevant declared families: 130/135; syntactic consumer sites: 1338/1435; enclosing checks: 219/214.

The adapter records imports, annotated fields, constructors, writes, references and checks. Five added members belong to incorporated selected context source, not new content-owner types. Raw base-name duplication reports two qualified `SelectedSource` declarations: compaction provenance versus captured delivery/AssignmentStore behavior. Reading their qualified imports confirms distinct roles; the count alone cannot resolve runtime dispatch.

## Existing owner implemented across consumers

`InputProvenance` remains the durable key/origin identity, with one constructor on `StoredInput`. The mistaken mandatory digest is removed. `RetainedTaskFacts.original_inputs` resolves captured `InputTaskFact.source` through existing fact-family hooks. `StoredInput.matches_original_source` owns identity/content comparison; `StartedInput.proves_started` takes that original row. No second digest roster, full-row control copy or replacement class.

`SelectedSummarySource` supplies that retained payload to reservation, interrupted recovery and native-start checks. Final admission rechecks `attempt.request`. `SelectedSummaryAttempt.request` is the sole current typed request; `source_json` remains the exact original hash proof, with only native hash/link consumers. Source/envelope/read compatibility methods remain deleted. Private coverage continues to use genuine input/native receipts.

Current production correction against context integration `9e3fafa9`: 27 lines deleted / 80 added, nine files, zero new types. Integrated branch delta against `a87a7065`, including producer and #511 ancestry: 718 deleted / 1092 added. The raw numstat separates that integration from the owned correction.

## Explicit limits and final validation

57/56 native JavaScript/TypeScript omissions are listed individually. SDK source, dynamic/generated/f-string code, receiver types and actual native C3 dispatch are not resolved by this Python AST tool. Local native AST parsers were unavailable; no package was installed. Arendt owns the native/source boundary. These outputs do not establish runtime correctness or installed readiness.

Actual certified-prefix presence from Mendel #510 found 23 historical context-manifest input references without a digest and zero native-input pins in that prefix. Restoring the existing identity contract preserves their format; original bytes and native proofs are not rewritten. Carry preserves UNKNOWN and issues no input admission.

Implementation preceded validation: 66 focused source checks passed in 5.35s; the earlier negative fixture batch is preserved. Final delivery requires one coherent Native6 installed saved configured fork through actual compaction/native/ACP. Public installation remains paused. No additional provider, public input, native donor or default was changed.
