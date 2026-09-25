# Native input routing for saved transcript presentation

Toad #34 exposed incoming agent text being reconstructed as an ordinary User
message. The live incoming envelope is attributed, but older saved native
inputs carry only their prompt text. Turn-wide annotations are written only
after success and can also misattribute a later steer to the initial sender.

## Per-input binding

The existing pre-send owner boundary now records the native input ID, exact
sent-text SHA-256, and incoming `TurnRouting` requests in the transcript store.
The route is taken from the committed envelope already held by the owner,
never inferred from a prompt header. Ordinary human/internal inputs receive
an explicit no-route binding, so a stale turn-wide annotation cannot turn a
human follow-up into another agent's message.

Readers use the binding only for native user records with matching ID/text.
It takes precedence over old entry-wide annotations. Changed text and a
conflicting rebind fail closed. No outgoing-success claim is made by an input
binding. Attribution survives failed turns, busy steering, reopen, and first
session-file creation without changing native start/delivery authority.

The additive `input_routing` table preserves the old two-column `input_display`
schema so existing writers continue to function. New display/routing records
commit in one SQLite transaction. Ordinary transcript reads use indexed input
IDs, not full bus scans.

## Explicit repair of historical bound inputs

```sh
agent-comms --root /path/to/wire repair-input-routing
agent-comms --root /path/to/wire repair-input-routing --apply
```

The default is a read-only preview. Repair joins existing owner-persisted
native-ID/input dispositions to committed bus rows by sequence, checks the
target and exact originally recorded source prompt, and preserves the bound
sent-text digest. A compatible channel batch is recovered as one native input
only when all records share the owner/admission/turn/sent text and the exact
ordered batch matches the sent suffix. Ambiguous duplicate IDs, conflicting
existing bindings, missing rows, and mismatched receipts are skipped/reported.
It does not infer identity
from similar text, acknowledge inputs, advance read/delivery cursors, rewrite
transcript files, launch models, or retry UNKNOWN inputs. Repeated repair is
idempotent. This is explicit maintenance, not an added wake/send-time scan.

Older records without native-ID receipts cannot be reconstructed by this
operation. No live repair or runtime restart is implied by this PR.

## Validation checkpoint before latest-main reconciliation

- 49 focused routing/display/index/input-authority/paging/legacy tests passed.
- Full run on the first candidate: 1,389 passed, 36 skipped, 3 failed.
  The same three ACP tests fail on unmodified base `a38d554`: they expect no
  extra goal-only `SessionInfoUpdate`. These are not attributed to this patch.
- Whole-tree lint/type checks also found pre-existing goal-history/runtime
  issues outside this slice. Do not describe the whole suite/quality gate as
  green from the focused results.

Test sources: `tests/test_transcript_input_routing.py` and
`tests/test_input_routing_repair.py`, plus adjacent existing tests. All new
tests are offline and use disposable wires/fake backends.

## Latest-main reconciliation

Merged channel-delivery PR #51 and stack-pin PR #57. Preserved the new durable
per-recipient channel keys, compatible channel batches, aliases, start gates,
and no-replay behavior; the former unsent-string fallback is not extended to
the newly durable channel commands.

Focused current integration: 60 passed / 8 skipped without the optional native
stack executable. Enabled that executable against the test's localhost-only
SSE endpoint and nonlocal-fetch guard: all 8 native cases passed (delivery,
batch, busy steer, rename, renamed batch, and goal/stop/reopen refusal). Added
replay assertions verify exact incoming message IDs/bodies from real native
session files, not only fake callback events.

A read-only preview on the live wire found 68 eligible native inputs, zero
conflicts/skips, and wrote zero repairs. No live apply, prompt, ACK, or restart
was performed. The full-suite checkpoint above is historical until a new full
run completes on the combined head.
