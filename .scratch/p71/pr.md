## Working selective queue clearing; net-negative product change

Core head: `ead8e2def1cc0dd38da2b8f6d1c54058ed47cc6c`, with main `6348c34197025d3d764d9f3e3e33055535454981` merged normally, preserving the new P18 fork and P41 FULL changes. The private installed acceptance below is on its pre-integration candidate `e730922ce`. No source budget trailer and no new helper module.

### Independent main-based sizes

| PR / comparison base | src additions/deletions | tools additions/deletions | tests additions/deletions |
|---|---:|---:|---:|
| This product PR, main `6348c3419` | +314 / −317 (**−3**) | 0 / 0 | +128 / −272 (**−144**) |
| Tooling #1095, its original main base `00be560e8` | 0 / −948 | +947 / 0 | +18 / −19 |

The product result is also **−3 excluding `src/agent_comms/debt_ratchet.py`**. #1095 is merged, owns the relocation, has budget zero, and earns this PR no deletion credit. Its checker finds renames globally at 50% before filtering source paths: main `73a52fbc2` to rejected `eed21e60c` correctly fails at **+187**, rather than counting a moved file as product shrinkage.

### Behavior and ownership

- `InputReservation` owns the original item, stored row and transferable rollback scope. The item's captured context owns its source identity; unused reservation owner data is gone.
- Inbox members dispatch themselves. The common `TurnInboxItem` owns the empty carried-input behavior; `InputRequest` owns its one actual carried identity. Controls do not introduce inputs or raw dictionary commands.
- Clear selects user inputs, preserves bus and original inputs, waits for original prompt ACKs, and asks native to remove exact IDs. Only confirmed native removals retire native-bound input. Terminal cleared records retain original native ID, digest and turn; clearing never grants a retry.
- `InputForwarding.pending` is the sole native-ID index of original `Prompt` objects. No copied pending record, text identity, marker fallback, or second clearing sweep.
- Native queued-input clearing is carried by the existing steering patch, with explicit requested/returned IDs. SDK intentional full-clear behavior remains separate.

### Decisions and competing paths deleted

- Deleted the unused `InputOutcome`/`UnansweredReason` schema. Actual durable outcomes stay with `InputAttempt`; no parallel outcome roster remains.
- Deleted the advertised-but-fake crash-recovery role, its work/disposition types, required-role entry, and Pi's empty listing / failing settle / no-op resume. Actual input recovery remains with durable input and assignment owners. No UNKNOWN row was replayed or altered by this deletion.
- Deleted `DeferredQueuedInput`'s unread stored-row/turn fields, both copying conversions, per-item fake turn rebinding, and request `accepted()` dispatch. Queue versus steer now differs only in the actual control it enqueues. Original echo/restoration behavior remains on the item.
- `InputDocument.transition` now also owns single-row transition/refusal; the store no longer repeats that decision. `StartedInput.matches_lease` serves native-evidence and delivery-reference consumers. The unused provenance query pair is gone; the required check remains on `StoredInput`.
- All request control producers use `Queue[TurnInboxItem]`, not `Any`.
- Deleted obsolete implementation/stub tests and the case that bypassed the typed ingress boundary by directly putting a list into `QueuedInput.text`. Real oversized queue projection cases remain; no external malformed-input contract was weakened.

### Source evidence

Reused NRA `ModuleSyntaxIndex` before the structural pass across Core, paired Toad and pinned audit roots: 1,071 modules, 5,144 relevant declaration/import/write/decision sites, zero parse omissions. After the deletion pass: 1,071 / 5,122 / zero. Integrated-main census before the final P18/P41 merge: 1,064 / 5,128 / zero. Main's independent deletions explain the changed module count. The unchanged native source census covered 362 modules without parse errors.

Static syntax does not prove arbitrary dynamic aliases. Reviewed the existing declaration-derived backend discovery and the test's dynamic role-name lookup; removed names have no remaining production/paired-Toad references. No alternate decoder or compatibility alias was retained.

Owners/consumers: `agent_backend.py:98`, `acp_extension.py:562`, `queued_input.py:287`, `input_attempt.py:309`, `input_attempt.py:327`, `input_disposition.py:64`, `input_disposition.py:283`, `input_drain.py:577`, `turn_inputs.py:94`.

Searches: `rg 'RecoversAfterCrash|InterruptedRequestWork|InterruptedToolWork|UnansweredReason|InputOutcome|DeferredQueuedInput|TurnInboxControl|matches_original_provenance|reservation\\.owner|request\\.accepted' src tests <paired-Toad>/src`; exact main-based `git diff --numstat BASE...HEAD -- src ':!src/agent_comms/debt_ratchet.py'`; AST outputs retained under `.scratch/p71/input-family-*.json`.

### Verification and remaining acceptance

- Exact-head ratchet against the merged main: **exit 0**, SourceLines **−3**, no positive per-file measure, no introduced frozen write or banned vocabulary.
- Final focused input/queue/backend families: **35 passed, 2 skipped**; old RPC stub excluded. Earlier unchanged tooling guard family: **92 passed**.
- Undefined-name/unused-import checks and whitespace checks passed. Full lint is not claimed; inherited style warnings remain.
- Earlier installed Core `b458716c3` / native `70f0512abbcfa4e0`: real held user+bus selective clearing passed; actual CLI start/send/history/inbox/stop proved one provider request, one native input and one reply, with no residual task/pending state. That is retained evidence for the earlier candidate, not current-head installed acceptance.
- **Fresh installed e730 candidate PASSED:** Core `e730922ce` / Toad `16de888e2` / native70, runtime `20261017T014032Z`. The real held user+bus clear passed in **13.34s**, preserving original native binding and running the original/bus once without the cleared user. The actual CLI start/send/history/inbox/stop journey, with a controlled provider, passed with **one request, one native user, one native input and one reply**. CLI stop confirmed; the clear fixture retired its children. Fresh private roots, no source overlay or replay. Logs: `.scratch/p71/installed-e730-clear.txt`, `installed-cli-e730.txt`, `cli-e730/receipt.json`.
- Final P18/P41 main integration is pushed at `ead8e2def`; its exact-head ratchet and undefined-name/import checks pass. The fork method is unchanged relative to the new main. This final integration has not been separately installed; default installation remains Codex's operation. Shared pointers remain unchanged.
