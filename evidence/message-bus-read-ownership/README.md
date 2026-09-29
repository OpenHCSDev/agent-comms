# MessageBus source, membership and bounded paging ownership

Dalton; persistent comms-wire-receipt-identity-20260929, based on corrected379 plus main69be5605. Parent owns live integration; no live changes, CI deferred. Exact installed refactor-audit skill and relevant catalogs matched authoritative archive bytes; NRA/global AGENTS reread.

## Ownership and actual deletion

Delete MessageBus history_manifest/history_sources/attach_history/historical_page, _scope_filter, _history_page/_indexed_history_page/_collect_history_page. Existing MessageDisplayScope is the sole inclusion/candidate-reduction contract. DeliveryScope now implements it; inbox selection uses captured Channel/DM scope instead of anonymous predicates. Full history uses the same scope owners. Human read-ACK and incarnation authority remain independent of incoming delivery.

MessagePageRequest captures membership, exclusive traversal and budgets once. Older/Newer traversal declarations own source order, cursor boundaries and edge meanings; PageWindow owns the shared progress/budget answer. Validation no longer repeats between indexed/scan paths. Both readers preserve a single oversized row and never skip the next forward eligible row. New ReviewScope declaration works through both readers without a central member roster. IMPL-4/5/12, IMPL-8, IDEN-1, BOUND-1. No codec subclass/cache/mixin introduced.

HistoryArchive owns the existing ordered retained-source manifest, atomic locked snapshot publication and source-bound traversal. It decodes the declared tuple once and opens source WireLog directly, without constructing a delivery MessageBus for browsing retained history. Same durable manifest/source/publication format and frozen source aliases/membership; no converter/reset/admission authority added. Current source revisions, guard/marker sealing, strict public decoder and canonical wire lock retained. No durable lock removed. TIME-9/BOUND-1.

GoalReplyScope owns the repeated captured wait reply predicate. Both real goal callers migrated. HistoryViews caller replacements are narrow; no target presentation or read-ACK redesign. Transcripts and all maintained core helper callers migrated. Paired Toad current-main helper has one manifest caller replacement; no Toad production caller of removed API found. Public Comms.views API unchanged.

## Verification and limits

- Focused source:141 passed16.15s (paging/source corrupt offsets+registry identity, delivery rebound/alias/index outage, current goal hooks, channel scope, admission and S4 guards). New-family scope tests included.
- Physical installed native fork:1 passed14.00s. Actual parent24%, bytes/tokens mismatch retained, ordinary production fork/ACP first answer8.869s,0compactions,exactly2 local HTTP provider calls,1first input/no replay, parent bytes/session preserved, durable relationships/history exercised. Installed own noneditable core; natived396, no user state.
- Auxiliary cursor old102 assertion reproduced RED; current published reply advances certified prefix103, injected seq remains exactly original. Corrected test passes1 in4.17s; fake provider strength explicitly limited to prefix behavior. No native cursor implementation edited.
- Touched production ratchet: no chain-term/foreign-absence/codec-subclass/excess increase. MessageBus now412 physical class lines (below500); no new class crosses500. ratchet.json records per-file changes, not global zero-debt claim.
- Initial focused runs lacked pytest-asyncio/native-package env; these failed attempts retained. Wider declarations probe also exposed4 existing unrelated error-message/commit-uncertainty expectations; not folded into read authority changes. Precise assignment/diagnosis will be reported, not hidden as green.
- Continuous installed current-Toad saved/native journey is running; readiness awaits actual exit. Initial invocation mistakenly placed nonexistent ToadWT venv before old live imports and failed before native input; preserved environment RED, corrected command uses actual coreWT installed path. No product patch/weakening for setup failure.

Remaining existing MessageBus publication orchestration/pending aggregate caches are retained authorities; no new cache introduced. Universal whole-project god-owner/latency/coverage requirements are not claimed closed by this scope.
