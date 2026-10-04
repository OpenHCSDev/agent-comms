# Original input request measurement reader

Existing RecordedNativeProbe.observed_requests acquires original diagnostics once
and groups the matching recorded turn/session/input by original request ID. The
old acquisition-time final-request filter and all its method callers are gone.
Only the manifest-selected group feeds existing SDK/model/terminal comparison.
Every retained group borrows the same budget/timing behavior. Earlier requests
never inherit the final manifest's digest, body or completion.

Four affected controls passed in0.37s: original JSONL and FieldCodec boundaries,
interleaved first-seen ordering, retry allowances, original zero counters, absent
budgets/capture, wrong turn/session/input and artifact mutation, plus preserved
selected-request model/completion/alignment. Authored records qualify reader
plumbing, not a configured multi-request model turn. No original/SDK/provider
read or execution, package, environment, native artifact or loan.

Existing AST tool parsed734 src/tests/tools modules with zero omissions. After
migration there are no old singular method definitions or attribute callers.
Three local Name sites still name the selected tuple inside construction; these
are not stale method callers or an independent authority. Before/after output
is lexical source evidence, not proof of dynamic resolution.

Capture remains optional: observed request groups do not prove all requests or
retries were retained. Native elapsed/callback values remain original; no clock
subtraction or aggregate whole-turn duration. Runtime/native producers and all
prior frozen observations are unchanged. Full S4 and the30-pair/USD75 study
remain unfinished/unapproved. No billing, capacity, HTTP or independence claim.
