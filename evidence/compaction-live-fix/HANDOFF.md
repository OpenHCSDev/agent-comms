# Live compaction failure — active correction, not ready for activation

User reported agent-comms-ux automatic summary limit_exceeded and manual compact
refusing its missing owner journal bridge. Actual Toad log is available under
~/.local/state/toad/logs; owner errors do not require user relay.

Parent owns this installed-PR225-base branch. Darwin owns the manual journal
bridge, durable decline outcomes, and Python timeout/progress contract. Lovelace
is auditing remaining semantic caps versus transport/resource budgets. The full
refactor continues separately in PR229; this correction must not wait for D22.

## Reproduced on the actual retained session

The session has 28,087 native records, 312,227 current context tokens, 776 messages
to summarize. Preparation serializes 2,096,910 bytes of raw objects but only
801,776 bytes of actual summary source. Existing selected wrapper rejects raw
objects at 1MiB before native compaction's model-sized chunk plan can execute.

Actual CLI + get_state + preparation + selected RPC on a copied 143,686,055-byte
session reproduces ready -> declined(limit_exceeded), zero transport calls.
After deleting that redundant cap, the real chunker completes seven loopback
provider requests, then the wrapper rejects 601 read-file records against 256.
The correction removes duplicated file-count checks from selected RPC,
Python SummaryFiles, owner commit, native commit helper and SessionManager metadata; also removes the
aggregate 64KiB metadata cap from commit. No cap is increased. Current candidate
returns summarized through the real native CLI/RPC and seven model-sized requests.

The local HTTP server substitutes only provider responses. Native loader,
retained history, settings/model selection, request construction, SDK stream,
compaction plan, reduction and response validation are real. This is NOT paid
provider or live-owner acceptance. The latest retained-journal-current probe also exercises the actual owner journal,
native commit child, and SessionManager reopen: committed, 601 read-file records,
211 modified-file records, 46 restored context messages. All original
session/root/user input files remain untouched. Initial copied-file
permission failure and the second-cap failure logs are retained.

## Still required

- Full ACP manual bridge with retained session; direct journal commit/reopen passes.
- Existing input remains UNKNOWN; no automatic resend/replay or journal deletion.
- Systemic capacity closure: cumulative output, overall deadline, response framing,
  retained snapshot bounds and session loading must follow their actual owners.
  Do not replace one arbitrary total-history ceiling with a larger ceiling.
- Installed candidate wheel/native package acceptance, then idle-owner activation.
- Fold the correction into refactor229 and retain current ownership/deletions.

## Capacity ownership

The existing native CompactionPolicy and compact() chunk/reduction plan own
per-call model capacity. There is no duplicate total-history, file-count,
cumulative-output or total-elapsed-time budget in the selected wrapper. Final
provider usage is checked against the actual native call's maxTokens. Correlated
real stream progress renews the existing Python transport inactivity grace;
no timer heartbeat can keep stalled work alive. Framing and retained-loader
resource bounds remain under audit, not silently removed from their owners.

Owner instruction 2026-09-28: "alwayus polymorphism, thers no such thing as too much polymorphism".
Behavioral variants belong to subclasses of the existing public abstractions,
with inherited common mechanics and replaced switches deleted. Manual and
automatic paths must share the existing selected transport/journal writer.
