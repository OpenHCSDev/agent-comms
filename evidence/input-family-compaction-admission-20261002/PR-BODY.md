## Fix the input-family compaction admission conflict

The original boundaries input `acp:0d9d2784d0f34858947e138b375e83e5` is durably NotSent. Its existing owner permits compaction through `unsettled_for`, while the continued-session verifier independently treats its user-attention `unresolved` flag as delivery uncertainty. The earlier actual540 read retained this refusal; current live activity lacks its inner traceback, so that historical cause is not asserted as recovered.

Reuse the existing InputAttempt/InputDocument admission behavior across source coverage and reservation. Confirmed unsent input remains recorded and is never replayed. Actual uncertain native delivery continues to block across admission changes. Keep raw markers, recorded ancestry, fork publication and source proof unchanged. No new state, reader, codec or compatibility path.

Einstein contributes this family under Mendel runtime integration. Mendel owns545 native source cursor/proved coverage/historical native inputs. Shared-file grant precedes edits. Source and AST semantics come first, one coherent implementation batch next, affected sanity and one configured saved-fork journey last. No new worktree or environment; original inputs, failed receipts and public stores remain protected.

## Source checkpoint complete

The existing bound-input parent SentInput now owns native-delivery uncertainty; BoundUnknown inherits it, and Started overrides only after the original native-start receipt. StoredInput owns confirmed non-delivery, so NotSent inherits permission while keeping its notice and no-resend behavior. ReservedInput owns its captured pending-original exception. InputDocument owns one shared compaction readiness operation used by both the canonical selection and the continued-source verifier. The admission-generation/attention flag reconstruction and redundant NotSent override were deleted.

Production35 added/20 deleted lines across three files (net15); no new type, field, store, codec or durable format. Exact declarations and consumers are in before/after AST:311 modules,0 omissions; one readiness declaration and one unsettled_for consumer. This is source evidence, not dynamic proof.

Final affected validation:77 checks passed in3.47s, then2 controls passed in0.13s after moving uncertainty to its existing binding parent. SOURCE-READY.json pins each source/log and explains the concrete failures checked. These are source controls. Mendel owns ONE joined configured saved-fork plus actual selected/cursor installed gate with545. Sch owns refresh of a released existing receiver; native comes from the joined source declaration (960296), no new environment or native copy. Installed/live readiness remains pending that joined journey. Original0d9/79cb/418 and raw failures remain unchanged and are not retried.
