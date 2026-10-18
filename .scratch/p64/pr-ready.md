## P64 / P46

Extends the existing packaged ratchet. No second scanner, measure-name registry or file exemptions.

### Ownership
- Six concrete `Measure` declarations are discovered by the existing family: `FrozenFieldWrite`, `BannedVocabulary`, `FunctionLocalImport`, `LiteralGetattr`, `ForeignFieldSpellings`, `BroadExceptDefault`.
- `CleanChangedLines` composes the stricter policy into frozen writes and vocabulary. Per-file totals may not increase; added/changed code must contain zero matches even if deletions lower the total. Untouched old sites are not forced to change. A moved occurrence cannot be paid for by deleting the old file.
- `ChangedSource` owns head bytes, parsed AST and new-side Git hunk lines. Diff context settings cannot mark unchanged lines as new. No raw-source regex.
- Vocabulary terms are declared once as a ClassVar tuple. AST owns identifier/docstring classification; Python tokenize owns comments. One match per identifier and per offending prose line. Ordinary string values, including durable record keys, are data, not prose. No durable schema rename or cutover is attempted.
- `Comparison.failed` derives exit status from increases and written-line matches. Deleted the previous growth-only decision; CLI is its migrated consumer.

### Real-history CLI proofs

Each invocation used `python -m agent_comms.debt_ratchet` and exited 1 on its own measure:

| Measure | Rejected head | Before → after |
|---|---|---|
| FunctionLocalImport | Core `7d0f27397`, merge-base `979bb0f04` | six files increase, including goals.py 5→6 and thread_execution.py 4→8 |
| ForeignFieldSpellings | Core `9a6f3544a`, merge-base `8728ed6dc` | tool_declaration.py 0→1 |
| FrozenFieldWrite | Core `f83053f6a`, parent `cdbe8e903` | native_settings.py 0→2; two written-call matches |
| BannedVocabulary | Toad `c73a10533`, merge-base `2efe00731` | widgets/wire_history.py 0→4; wire_history.py 0→1; five written prose lines |
| LiteralGetattr | Core `1a62c4e16`, parent `6090de486` | locked_store.py and mro_dispatch.py each 0→1 |
| BroadExceptDefault | Toad `652cd9924d`, parent `d84c4df10` | history.py 0→2 |

The originally requested Core `3adfdaa6d` is retained as an invalid vocabulary witness: it has no counted increase. It was replaced with Toad's witness following owner correction 14929, not counted as a passing proof.

These are explicit syntax rules, not proof that every historical match caused a behavioral bug. Field-spelling tuples are literal identifier tuples used by a for/async-for/comprehension; declaration tuples are not counted. Broad-default checks cover bare/Exception/BaseException handlers returning None/false/zero/empty literals, without attributing nested function/class returns to the handler.

### Verification
- Full guard file: **91 passed** with the installed audit dependency.
- Added Git-context regression: **2 passed**. Existing assertions unchanged; no skip/xfail.
- Earlier run with the checkout's older audit package: 85 passed, 6 failed; retained. Pairing only the installed `refactor_audit` package corrected the dependency mismatch without installation or changed assertions.
- Candidate `5042bb4e0` against current remote main after normal integration: **ratchet exit 0**, no positive deltas and no introduced matches.
- NRA: **341 production/dependency modules**, zero parse errors; declaration/inheritance/function and consumer references retained. Dynamic alias resolution is not claimed.

Results: `/home/ts/.cache/agent-scratch/rts-results/p46.json` (six gates), `p64.json` (both requested measures and tests). Full CLI outputs and NRA evidence: `/home/ts/wt/core-doctrine-segment/.scratch/p64/`.

No installation, original-input replay or change to the blocked P44 goal.
