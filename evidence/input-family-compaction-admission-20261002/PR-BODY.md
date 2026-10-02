## Fix the input-family compaction admission conflict

The original boundaries input `acp:0d9d2784d0f34858947e138b375e83e5` is durably NotSent. Its existing owner permits compaction through `unsettled_for`, while the continued-session verifier independently treats its user-attention `unresolved` flag as delivery uncertainty. The earlier actual540 read retained this refusal; current live activity lacks its inner traceback, so that historical cause is not asserted as recovered.

Reuse the existing InputAttempt/InputDocument admission behavior across source coverage and reservation. Confirmed unsent input remains recorded and is never replayed. Actual uncertain native delivery continues to block across admission changes. Keep raw markers, recorded ancestry, fork publication and source proof unchanged. No new state, reader, codec or compatibility path.

Einstein contributes this family under Mendel runtime integration. Mendel owns545 native source cursor/proved coverage/historical native inputs. Shared-file grant precedes edits. Source and AST semantics come first, one coherent implementation batch next, affected sanity and one configured saved-fork journey last. No new worktree or environment; original inputs, failed receipts and public stores remain protected.
