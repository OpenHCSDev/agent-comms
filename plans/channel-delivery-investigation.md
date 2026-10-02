# Channel delivery and latency investigation

Branch: `investigate/channel-delivery-performance`, integrated onto main `a38d5544`.
User report: missing or slow interthread messages in `#nra`.
No live messages, provider calls, owner restarts, or delivery acknowledgments
were issued by this investigation.

## Confirmed cursor bug and candidate fix

Forward bus pagination previously continued searching after an eligible row
exceeded the remaining byte budget. With small/large/small messages, it could
return sequences `[1, 3]`. ACP then persisted cursor 3 and never fetched 2.
This affects channel history, DMs, and owner incoming delivery.

Stop the forward page at its first excluded eligible row. The next page starts
with that row; a single oversized first row is still permitted so pagination
can progress. Backward paging is unchanged. Six persisted-bus regressions fail
on the original code and pass with the fix. The focused paging/routing/sequence
selection passed 31 tests. This establishes this cursor property, not general
channel reliability. No evidence yet ties this byte-budget bug to the reported
live `#nra` messages.

## Live evidence, 2026-09-25

Read-only correlation of bus sequences 6773, 6779, 6785, and 6790 against native
user-message entries in the five current `#nra` members' session files:

| Recipient | Publication to native user entry, seconds |
| --- | --- |
| nra-architecture | 5.282, 5.905, 5.594, 5.463 |
| nra-domain-mapping | 3.236, 3.671, 3.304, 3.061 |
| nra-roster-heuristic-map | 3.471, 3.763, 3.670, 3.556 |
| nra-class-first-name-review | 3.439, 3.710, 3.560, 3.237 |
| nra-ordered-branch-call-review | 3.448, 3.653, 3.524, 3.196 |

For sequence 6790, activity first entered `thinking` 1.22–1.46 seconds after
publication. Native user entries followed at 3.06–5.46 seconds. Thus timing
includes owner admission/launch overhead before model response time; reply
latency alone cannot diagnose the bus.

The identical pause text was published twice, sequences 6791 and 6793,
51.325 seconds apart. Matching native entries in four recipients appear
94.96–120.44 seconds after the first publication, or 43.63–69.12 seconds after
the second. Content correlation alone cannot distinguish identical sends.
No matching entry was found in nra-architecture's currently selected session;
its ACP log then resolves that absence: two client `session/cancel` requests
surround the queued channel turn's start/settle events (lines 196–200 in
`Agent_Comms_2026-09-25T11_03_23_770030.txt`). The queued turn began at
1790348709.204 and was cancelled before a native user entry was recorded.
That is not evidence of spontaneous loss and must not authorize replay.

Reproduce the read-only correlation (JSON output includes ambiguity and limits):

```sh
PYTHONPATH="$PWD/src" python benchmarks/trace_channel_delivery.py \
  --root /home/ts/.agent-comms --channel '#nra' --after-seq 6772 --last 30 \
  --recipient nra-architecture --recipient nra-domain-mapping \
  --recipient nra-roster-heuristic-map --recipient nra-class-first-name-review \
  --recipient nra-ordered-branch-call-review
```

The local capture is `/var/tmp/ac-nra-delivery-trace.json`. It is deliberately
not committed: native paths and potentially sensitive session details belong
in local diagnostics. Explicit recipients avoid treating today's membership
as historical membership. A session entry is not proof of provider consumption
or successful UI display.

## Lifecycle findings and implemented changes

1. Human channel messages have a `reply_target` and wait in `_pending_turns`
   until the current turn releases `_turn_locks`. Agent mentions have no
   `reply_target` and may enter an active backend's steering queue. A later
   agent instruction can therefore reach the model before an earlier human
   channel instruction. Define and verify ordering without losing reply routing.
2. Fixed: every turn-starting channel recipient now persists UNKNOWN before
   advancing its delivery cursor. Exact native input ID and text establish
   STARTED. Stop, goal activation, and crash/reopen leave UNKNOWN without replay.
   Late subscribers receive the projection. Toad visibility is a separate
   follow-up: protocol delivery alone does not establish visible UI display.
3. Goal activation still prevents unauthorized queued sends. Each affected
   channel request now retains its durable per-recipient UNKNOWN disposition.
4. Fixed: committed mentions resolve through current owner aliases under the
   admission snapshot. Wire membership remains authoritative. Native tests cover
   rename-before-drain, including a compatible batch. Batch receipt validation
   reads the exact admitted prompt rather than reconstructing renamed guidance.
5. Ordinary unmentioned **agent** channel messages are informational and never
   enter recipients' model context. Human channel messages are collective.
   The newer bounded-triage resolver is shadow-only. Clarify this intentional
   policy separately from data loss; do not quietly enable broadcast loops.
6. Fixed warm tail scans with a disposable SQLite offset index. JSONL remains
   authoritative: selected rows are reread and validated. Append is incremental;
   replacement/truncation rebuilds, and incomplete tails or malformed ordering
   fall back to the original collector. Cold rebuild still scales with history.

A copied real-wire profile returned just sequence 6796 after cursor 6795 but
decoded all 2,652 bus rows (2.62 MB). Six warm reads had a median **35.01 ms**;
seven reads ranged 34.35–36.75 ms. This scan holds the bus lock. Multiple owners
waking on the same publication can therefore serialize repeated full scans;
the cost scales with history and owner count. This is a measured backend cost,
not yet a measured decomposition of the full 3–6 second live delay. Local
profile: `/var/tmp/ac-nra-incoming-page-profile.json`.

## Next measurements

Capture publication, owner observation, queue admission, native matching start,
and visible delivery as separate timestamps. Test idle, busy, compacting, and
goal-running recipients, short and long histories, and multi-page bursts. Use
isolated real wire/ACP/native paths; keep model latency separate from transport
latency. Track unresolved outcomes explicitly across stop/restart. CI may run
asynchronously; it does not replace live-path reasoning and user testing.

## Integrated performance and native-path evidence

On a private copy of the actual wire (2,685 rows, 2,649,529 bytes), one incoming
page after sequence 6828 returned sequence 6829. Cold index construction took
77.323 ms. Ten warm reads had median **0.770 ms indexed vs 37.857 ms scanning**;
the indexed route decoded two rows and returned identical results. Local
artifact: `/var/tmp/ac-index-real-wire-profile.json`. This measurement excludes
Pi startup, provider response, and UI latency; it is not an end-to-end claim.

Native tests use the installed Pi executable and a localhost SSE endpoint, with
nonlocal network calls refused. They cover channel delivery, steering, batches,
rename plus batch, goal activation, stop, and reopen; hard process-exit tests
cover the durability gaps around cursor advancement and queue population.
Compatible channel requests share a native turn: reliability does not add one
model turn per queued message. Only an exact matching native receipt credits
every distinct sequence in that batch. No automatic replay is introduced.

The index received an independent review at eb374cc, including randomized page
oracle comparisons. CI runs asynchronously; deployment uses focused local
checks and the user's live testing, without waiting for all-platform CI.
