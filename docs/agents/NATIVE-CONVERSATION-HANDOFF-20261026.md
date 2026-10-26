# Native conversation handoff — 26 October 2026

Requested by Tristan so another Codex agent can continue without this chat.
Written by `comms-commander-2`. These are observations, not a declaration that
all the work is finished. Source, installation and a working live conversation
are separate results.

## Current status — 26 October 2026, after the paired upgrade

This header supersedes the original next steps below. The rest of this document
is the historical handoff snapshot; its exact-head test and source receipts are
preserved, not claims about the current build.

- [#3480](https://github.com/OpenHCSDev/agent-comms/pull/3480) is **merged** at
  `0054945a5aaf42d35396207e847b60aa744a4633`, from corrected source head
  `e9b6941acf66d397f1783ff51e459bf286e068d9`. The inherited queue typing and
  existing `TurnOutput.clean` corrections passed six non-skipped hosted checks;
  smoke/install rehearsal were skipped. The historical **38-test result belongs
  only to `0a58d2d60`**, not automatically to the corrected or installed build.
- [#3484](https://github.com/OpenHCSDev/agent-comms/pull/3484) is **merged** at
  `d9ef01241af2f2bb95e7a464f5cdc1c43fb22e0d`: preserve previous compaction history
  and stop a pending original prompt after cancellation. The earlier suspected
  summary-loss branch is no longer merely an unreproduced lead.
- [#3486](https://github.com/OpenHCSDev/agent-comms/pull/3486) is **merged** at
  `2e4aba13f3b0dd77f09fb1b6c9278e44e37ba586`: allow Send now safely through
  active automatic compaction. These repairs do **not** establish the cause of
  the separate approximately 50-second provider/stream interval.
- `codex-latency` accepted the handoff with its `channel_notifications` worker
  and owns the continuing history/start/compaction repairs. There is no parallel
  compaction implementation here. Gaming `codex-greentext` owns separate
  CLI/model/import work; the old pending `claude-code` review contact below is
  historical, not a request to start another review of #3480.
- The current runtime link was independently read as
  `core-b1a8007f0-lock-7759b8e649381bf6`. Installer message 1588 reports the batch
  completed in **57.1 seconds / 100 phases**, no pending carries, two changed
  store reconciliations and all seven stopped owners restarted. `codex-latency`
  confirms this batch includes #3480/#3484/#3486. This is installation evidence,
  not proof of the fresh configured-provider/UI journey. An already-open user
  Toad window can still be running the old `cbb` build; it was not killed.
- `codex-latency` reports **4/4 paired native qualification journeys passed**
  before installation: stopped saved-tab Start with fresh DM/reply/return,
  identical-answer reconnect, busy hidden two-tool-round history, and a copied
  fork's fresh DM/reply/return/reopen. Those are that owner's results, not a new
  test run by this document's author. The separate installed saved-tab → fresh
  copied-agent DM → configured-provider reply → tab-return check remains with
  `comms-commander-2` and is pending at this update.

No new diagnosis or tests were run for this documentation update. Do not replay
original failed/UNKNOWN inputs to qualify the upgrade.

---

## Original handoff: start here (historical)

1. Review [PR #3480](https://github.com/OpenHCSDev/agent-comms/pull/3480).
   Its input/response repairs are implemented and have passed 38 targeted tests,
   including actual native processes with a controlled local provider. They
   still need review and the installed configured-provider/Toad journey.
2. Read the compaction findings below and
   [issue #3477](https://github.com/OpenHCSDev/agent-comms/issues/3477).
   The long wait is measured. There is **no compaction repair yet**.
3. The possible loss of an older summary is a **new, unreproduced source lead**.
   Do not report it as demonstrated data loss. Reproduce the real preparation
   branch before choosing an implementation.
4. `codex-latency` owns the separate busy-tab history repair. Do not start a
   competing change to that code.

I retain the compaction investigation until a named agent accepts this handoff.
A Codex agent taking it over should identify itself and its branch to
`comms-commander-2`; this is coordination to prevent duplicate edits, not a
request for another design-approval gate. No compaction source edit is in flight
here. Gaming `claude-code` is the existing review/integration contact for #3480.

## What #3480 changes

Tested source commit: `0a58d2d60cd2b011784c5066dc0f790ba5363b52`, based on
`901a56f95528`. This separate notes PR is based on main; it does not contain
that implementation or extend its test result to a different revision.

- `TurnInbox.ready_inputs` derives runnable user-input IDs. A returned control
  command is not an undelivered user prompt. The old `has_work` flag is deleted.
- `InputForwarding` distinguishes inputs not yet written from requests already
  written to native but lacking final evidence. `QueuedInputNotSent` and
  `QueuedInputMissing` own those different failure meanings.
- `SettingCommand` uses the existing accepted/rejected outcome dispatch instead
  of its separate raw-boolean response path and fallback error strings.
- `Response.require_data()` owns guarded payload access. Six production callers
  use it. `PendingAttestation` still calls `.require_state()` afterward; an older
  saved patch omitted that newer check and must **not** be applied wholesale.
- Expiry-owned failure text had already landed on main and is not copied again.

Implementation diff before this document: 11 files, +117/-35. Production files:
`agent_backend.py`, `native_attestation.py`, `native_child.py`, `pi_commands.py`,
`pi_events.py`, `turn_failure.py`, `turn_inputs.py`, `turn_stats.py`.

Actual tests collected and passed: **38/38, 28.21 seconds, exit 0**, with one
existing pytest assertion-rewrite warning. Selection:

```
tests/test_turn_inbox.py
tests/test_prompt_response.py
tests/test_native_input_bus_path.py
tests/test_native_session_reopen.py
tests/test_native_settlement_scope.py
```

The native tests exercised saved-owner reopening, a DM and its context proof,
following input and clear, a stalled provider, cancellation before write and
during pipe drain, and busy forwarding. Provider replies were controlled locally.
This is not a fresh paid-provider response or physical Toad acceptance.

The completed run used the installed Python and its adjacent native launcher,
plus existing cached test plugins. `PYTHONPATH` selected the worktree's `src`
and `tools` before those plugins, including for child processes. No installed
package, model/provider setting or launcher guard was changed.

Earlier checks are retained failures, not passing evidence:
- A selection named a file already deleted on main: zero tests collected.
- An incomplete cached Python had no adjacent native launcher: 31 passed,
  seven native tests failed before starting native; zero provider posts.
- A preflight used nonexistent `RunningRuntime.current()` and stopped before
  tests. The existing API is `LaunchRuntime.running()`.

Before/after source inventories used existing NRA syntax owners: 1,618 modules
parsed, zero parse omissions. They are lexical evidence, not a proof of dynamic
module bindings or runtime behavior. Inventories precede unrelated channel
changes fast-forwarded into the branch before its implementation commit.
Do not rerun tests merely because this handoff document was added.

## Two different latency findings

### The approximately 50-second request: cause still unknown

Original request `d0f36195-2455-4713-9ce3-9f27ceaad352`:

| Observation | Elapsed |
| --- | ---: |
| Dispatch | 0.118 s |
| First provider event | 0.330 s |
| First projected SDK delta consumed | 46.983 s |
| Stream end | 49.867 s |
| Finished | 49.874 s |

The long interval is between the stream starting and the first projected SDK
delta. It has **not** been separated into provider queueing, generation and
intermediate handling. First event is not first token. Do not call this
“reasoning time.” A thinking-setting command was refused; nothing changed.
A fast local adapter fixture does not explain this live-provider interval.

### The thesis correction: 152 seconds waiting inside native compaction

First correction, bus sequence 939:

| Recorded event | Unix seconds |
| --- | ---: |
| Bus publication | 1792993020.2011743 |
| Native user-message creation | 1792993020.844 |
| Appended to native conversation | 1792993173.186 |
| Context receipt | 1792993173.243 |

It reached native's queue in **0.643 seconds**, waited **152.342 seconds** there,
then took **57 ms** from conversation insertion to receipt. This is not 153
seconds of Core-to-native delivery, and stale notification paint was not the
main delay.

Six original inputs, sequences 939/941/943/944/945/947, all append after the
same compaction entry in turn `c97ff54fd6b1481eb5d23efaf2583dad`. Their original
identities are retained in the private evidence; none was replayed.

Two consecutive compaction requests took **141.384 s** and **15.218 s**.
The first projected deltas arrived at 8.659 s and 9.203 s respectively.
The first request still had 132.724 s between its first projected delta and
stream end. This does not prove continuous token generation or assign every
interval to the provider. Parallelizing the two requests alone cannot remove
the first 141-second wait.

The original saved summary contained 32,956 characters / 33,171 UTF-8 bytes:
29,219 characters before the split-turn section and 3,707 after it. File lists
accounted for 2,461 characters. These are not provider token/rate measurements.
A receipt named `SOURCE_UNCHANGED=True` only checked file size against an end
offset; it was not an immutability proof. The source was opened read-only.

## Compaction source already traced

Current route inspection selected native package `.pi-native-990b053a216346d6`.
That proves current route selection at inspection time, **not** which modules
were loaded in the original failing process. Native source references below
are relative to that package's `pi-coding-agent` directory:

| Owner/site | Established behavior |
| --- | --- |
| `dist/core/agent-session.js:424-460` | Await compaction before the next assistant response; then refresh context/model/tools. |
| `pi-agent-core/dist/agent-loop.js:91-108` | Repoll steering after preparation returns. |
| `dist/core/agent-session.js:1332-1400` | Steering creates/queues the tracked user message; it does not interrupt summary generation. |
| `dist/core/agent-session.js:1020-1034` | Send now explicitly refuses during compaction. |
| `dist/core/agent-session.js:1580-1586` | `abort()` already aborts retry, compaction, branch summary and the agent, then waits for idle. |
| `dist/core/agent-session.js:2099-2245` | Automatic compaction owns preparation, generation, append, reload and continuation. |
| `dist/core/compaction/compaction.js:647-713` | Split-turn compaction awaits history summary, then turn-prefix summary. |

`pi-agent-core` lives under the coding-agent package's `node_modules`.
The existing build patch owner is `stack/native/patch-native-compaction-cap.py`;
do not edit installed JavaScript. Manual, automatic and overflow paths have
different continuation requirements. Read them together before implementing.
Nine specified JavaScript modules were parsed with Acorn, zero omissions.
That inventory is not whole-package dynamic-effect closure.

### New lead: previous summary may be lost on one split-turn branch

Source read immediately before this handoff:

- `prepareCompaction`, lines 579-630, captures `previousSummary`, calculates
  `messagesToSummarize` separately from `turnPrefixMessages`, and returns both.
- `compact`, lines 665-673, enters the split-turn path, initializes history to
  `"No prior history."`, and passes `previousSummary` to history summarization
  **only when `messagesToSummarize.length > 0`**.
- The separate turn-prefix summarizer does not receive `previousSummary`.

**Unproven condition to reproduce:** an existing compaction summary, a new
retained user turn split in the middle, no complete earlier turns to summarize,
and a nonempty turn prefix. Check whether the actual `prepareCompaction()`
produces that shape, then capture what its public `compact()` sends to the
controlled provider. A manually invented preparation object alone would not
prove the real path can reach it.

The next useful step is that SDK-level reproduction using a wholly synthetic
in-memory session, not Tristan's original saved session. Check that the older
summary reaches the provider and resulting context. No reproduction was run,
no repair chosen, and no data-loss claim established before this handoff.

A possible consolidation of history and turn-prefix summarization is only a
candidate design. First trace its full consumers and required context. It must
not discard a real distinction or be advertised as solving the 141-second wait
merely because it removes the shorter second call.

## What a compaction repair must preserve

- Each original input exactly once; queue creation and model-context receipt
  must remain visibly different facts.
- Existing retained-context bounds and valid resumed context; no cap increase
  or stale-context bypass to make interruption appear successful.
- No late summary commit after cancellation invalidated that work.
- Correct continuation for manual, automatic and overflow compaction.
- Existing `AgentSession` / agent owners carry interruption and resumption;
  do not add another queue, another abort mechanism or a cosmetic read badge.
- Verify the affected installed user journey after a coherent source repair:
  saved tab, genuinely new DM to a copied agent, actual configured-provider
  reply, switch away and return, with history and drafts intact.

## Installation and the missing live check

Latest independently read installation at handoff:
`core-901a56f95-lock-038a6e111e0d4a34`. Installer sequence 1204 reported 103
phases / 73.1 seconds, no pending carries, two store reconciliations and all
five listed owners restarted. That build **does not include #3480**.
A running process is not a successful live conversation.

A previous 71d saved-tab check loaded copied history but timed out at memory
admission **before sending a fresh DM**. Observed admission required about
5.078 GB (3 GiB floor plus measured unit-start peak); available memory was lower.
No admission guard was weakened and that uncertain startup was not retried.
Native unit tests passing does not fill this configured-provider/UI gap.

One read-only tool call returned UNKNOWN around an installer restart. The
already-completed test's log and exit file were later read directly; the test
and original inputs were not launched again.

## Other owner's tab-history repair

`codex-latency` reported a new reproduction: returning to a warm busy tab misses
messages saved while hidden; close/reopen catches up. Its source diagnosis
names `TurnOwner.captured_snapshot`, `TranscriptPublication.native_current`
and `source_bound`. This is **their report**, not an independently reproduced
result here. I have no production history edits and explicitly told them they
can proceed. Their existing reproduction is stronger than my older leads.

My partial older-build source pass found publication generation changes on
source growth, fences protecting tab/native-widget/cursor agreement, and that
ordinary text/tool events did not use the suspected turn filter. It did not
identify the new busy-state exclusion/clamp. Do not pass those old leads off
as a fix or a complete current-source trace.

## Find the private evidence without publishing private history

Laptop evidence root:
`/home/ts/.cache/agent-scratch/comms-commander-2/`

| File | Purpose |
| --- | --- |
| `OWNER.md` | Full retained work, preservation rules and earlier unfinished tasks. |
| `LATENCY-20261026.md` | 50-second request and retracted causal attribution. |
| `THESIS-COMPACTION-DELAY-20261026.md` | Six original IDs, timestamps, source trace and evidence limits. |
| `native-corrections-complete-owner-check.log` / `.exit` | Actual completed 38-test run, exit 0. |
| `native-corrections-current-{before,after}.jsonl` | Existing NRA-based lexical inventories. |
| `compaction-steering-ast.jsonl` | Nine-module JavaScript inventory. |
| `71d-LIVE-RESULT.md` | Incomplete copied-agent UI attempt; no fresh DM. |
| `live-history-profile-20261025/SOURCE-PASS.md` | Partial older-build tab/publication trace, not the new reproduction. |

Original request logs are under `~/.agent-comms/diagnostics/`:
`4f9c860f37f747a095cdc7e44713f75d.requests.jsonl:41-54` and
`c97ff54fd6b1481eb5d23efaf2583dad.requests.jsonl:15-34`.
The original compaction is in
`sessions/2026-10-25T07-50-28-226Z_01a1720a-1e82-7441-849d-94a82580d8a2.jsonl:846-852`.
These paths are on the laptop, not automatically available on gaming/desktop.
Ask the existing owner for bounded evidence if needed; do not commit private
session contents to Git.

## Do not destroy the saved work

- Implementation branch `fix/native-response-and-input-outcomes`, PR #3480,
  is published. Its tested revision is identified above; review is still
  pending. The clean isolated checkout at
  `/home/ts/wt/comms-commander-2--ac8b7b78e6634b8dad1497ab50a697c3--agent-comms`
  is being reused for this separate `docs/native-conversation-handoff-20261026`
  notes branch, based on main. Do not assume its current files are the #3480
  implementation. The notes PR does not change #3480's review head.
- Old checkout `/home/ts/wt/comms-commander-2--P3284` has 12 uncommitted,
  untested files (+110/-36), based on an older shared branch. Preserve them;
  do not push over that branch or transplant the patch blindly. Its binary
  backup is `native-four-review-corrections-preserved.patch`, SHA256
  `0046927c81a6ba71ef90deab847c813dad0792c66e1b1784608c656d391cbf1b`.
- Main's untracked `command_native.md`, scientific originals/current builds,
  sessions, auth, editor undo/history and failed/UNKNOWN inputs are protected.
  In particular, never replay original scientific inputs 411/414/421.
- Keep Toad #1250/#1258/#1260, Core #3375 and scientific import/migration work
  visible in `OWNER.md`; this handoff does not mark those tasks completed.
- No debugger on active owners, raw-store repair, installed-file patch,
  gratuitous reinstall, paid-provider/model change, or local full suites/ratchets.

## One unrelated tool refusal to retain

The first ownership reply to `codex-latency` was positively refused, not sent:
`HandoffKind.require_publishable` at `src/agent_comms/messages.py:523-537`
recognizes the word `fixed` even inside `NOT fixed`. Source was inspected;
the evidence-bearing corrected message was sent once. This is a source lead
for the message-tool owner, not a hidden successful handoff or a reason to
replay an uncertain operation.
