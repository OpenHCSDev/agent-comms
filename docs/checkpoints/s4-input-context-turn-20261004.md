# Configured submitted input / original recorded context

The private original reader currently asks SentInput.matches_native to compare against that same row's turn_id. Bind the expected turn and admission to the already selected ContextManifest's recorded turn instead, using existing StartedInput.require_started and SentInput.matches_native. Native ID/sent bytes remain checked. Acquire the original InputDocument once, keep direct-native/missing-manifest scope explicitly narrower, and never infer thread birth/process/full lease from rows that do not store them.

Same reused checkout. Existing capture/read/source-delivery/scorer/continuation consumers use this reader; no input/schema/lifecycle/provenance/manifest producer change. No original replay, SDK/provider/model, package/env/native/holder operation. Existing AST/semantic source first, affected private checks and CLI last. Implementation/validation pending; full S4 and separate30pairs/USD75study unapproved.
