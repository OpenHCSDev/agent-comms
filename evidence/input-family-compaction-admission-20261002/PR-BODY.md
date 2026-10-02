## Fix the input-family compaction admission conflict

The original boundaries input `acp:0d9d2784d0f34858947e138b375e83e5` is durably NotSent. Its existing owner permits compaction through `unsettled_for`, while the continued-session verifier independently treats its user-attention `unresolved` flag as delivery uncertainty. The earlier actual540 read retained this refusal; current live activity lacks its inner traceback, so that historical cause is not asserted as recovered.

Reuse the existing InputAttempt/InputDocument admission behavior across source coverage and reservation. Confirmed unsent input remains recorded and is never replayed. Actual uncertain native delivery continues to block across admission changes. Keep raw markers, recorded ancestry, fork publication and source proof unchanged. No new state, reader, codec or compatibility path.

Einstein contributes this family under Mendel runtime integration. Mendel owns545 native source cursor/proved coverage/historical native inputs. Shared-file grant precedes edits. Source and AST semantics come first, one coherent implementation batch next, affected sanity and the installed original-owner coverage check last. No new worktree or environment; original inputs, failed receipts and public stores remain protected.

## Source checkpoint complete

The existing bound-input parent SentInput now owns native-delivery uncertainty; BoundUnknown inherits it, and Started overrides only after the original native-start receipt. StoredInput owns confirmed non-delivery, so NotSent inherits permission while keeping its notice and no-resend behavior. ReservedInput owns its captured pending-original exception. InputDocument owns one shared compaction readiness operation used by both the canonical selection and the continued-source verifier. The admission-generation/attention flag reconstruction and redundant NotSent override were deleted.

Production35 added/20 deleted lines across three files (net15); no new type, field, store, codec or durable format. Exact declarations and consumers are in before/after AST:311 modules,0 omissions; one readiness declaration and one unsettled_for consumer. This is source evidence, not dynamic proof.

Final affected validation:77 checks passed in3.47s, then2 controls passed in0.13s after moving uncertainty to its existing binding parent. SOURCE-READY.json pins each source/log and explains the concrete failures checked. These are source controls. The installed original-owner coverage check below closes the specific admission conflict. Original0d9/79cb/418 and raw failures remain unchanged and are not retried.

## Installed original-owner coverage accepted — ready for scoped merge

Mendel exercised installed `PrivateInputs.require_source_coverage` on the joined source `c2a68017f7d3280e6a30789cd7c5d907b12f959a`. It passed in **2.454081 seconds** using the original `openhcs-audit-merged-boundaries` incarnation, unchanged NotSent input `acp:0d9d2784d0f34858947e138b375e83e5`, full 215-row input document and original **41,932,752-byte** saved session. A fresh child was not substituted. Native entry/header/ancestry/tracked-text/raw-proof/revision checks remained active.

Exact authored receipt: `evidence/input-family-compaction-admission-20261002/installed-original-floor/receipt.json`; SHA256 `16e18dab78356dc9de62e8f97da947581325da80b6783d0a07ab6154d7fccd69`. The original raw receipt remains at `/home/ts/.cache/agent-scratch/mendel-original334-coverage545546-20261002/receipt.json`. This is an authenticated read-only running-source snapshot, not a stopped carry.

All original source, proof and snapshot bytes matched before/after. The original NotSent record and attention notice stayed unchanged. Zero provider calls, native child processes, inputs, reservations, public writes or retries. The earlier failed original read receipt and all unresolved UNKNOWN proofs remain preserved.

**Scope:** the installed original source-floor check passes and this input-family patch is ready for merge. Public installation, a new compaction/prompt, full UI readiness and a recovered historical inner traceback are not claimed. No extra unchanged provider journey was run. Parent owns merge/publication; Mendel owns joined runtime integration.
