# Original S2 selected observation acceptance

Production base: PR325 a567b9fc. Own noneditable installed agent-comms package,
actual Pi bundle native-current-5fdef596596173bd, actual retained child/stdin/stdout,
existing native_backend local HTTP provider. No live root or provider settings changed.

`installed.log`: 1 passed in 6.38s. Test proves settings observation consumes
no provider call and writes no history; cancellation after the real response is
read retires the exact child; strict reopen on a NEW explicit input succeeds with
exactly two total provider calls/two saved user inputs, no replay; changed source
revision refuses observation before provider work. Only receipt delivery timing
is held, after actual stdio response; no mock endpoint or synthetic protocol reply.

Latest 22:16 refactor-audit and nra-refactoring reread. AGENT-8: reuse existing
native fixture and typed native/settings owners. IDEN-1/IDEN-3: assert existing
custody/revision capability rather than reconstructing internal field bundle.
TIME-9/BOUND-1: canonical production decoder unchanged. No production changes,
aliases, compatibility restoration, new codecs or registries. BooleanChainTerms
in new test: 0 (no BooleanOp chains). First attempts exposed test assumptions:
stale custody raises NativePiUnavailable before exchange; changing mtime also
changes ctime, so restoring mtime cannot restore revision. Final test places
stale-source refusal last, preserving actual revision semantics.

Not certification of every S2 predicate or universal size limits. PR325 review
corrections sent directly to Wegener: stale test callers, partial identity owner,
and retired fixture behavioral mapping. Parent owns integration/install.
