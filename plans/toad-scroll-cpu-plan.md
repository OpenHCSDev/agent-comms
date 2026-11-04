# Agent Comms CPU bursts: findings and structural repair plan

## Request and scope

Tristan, 2026-11-03: profile the actual Toad UI and its workers, then commit the
findings and a plan to main so a cloud agent can continue. His subsequent
correction defines the actual target: Agent Comms intermittently drives CPU to
maximum; scrolling itself is secondary. In his words: "sometimes agent comms
just goes crazy to max useage" and "i'm tryin got get this struturally sovled not
just good enough rn".

Find and remove the cause of those bursts across the UI, observer and runtime
workers. A quiet period, acceptable scrolling, lower process priority, a polling
throttle or an average below 100% does not establish the repair. This is an
implementation handoff, not a claim that the CPU problem is fixed.

The cloud agent takes this CPU investigation. Codex-latency retains the separate
socket-retry repair, Core #3991. Do not duplicate that repair or the independent
checkout-preparation repair held by comms-commander-2.

## Exact observed application

- Host: `ts`, Linux x86_64; Python 3.14.8.
- Installed Core: `ce3d3f889b8081ea106e3729169401d8889f20b4`.
- Installed Toad: `a49ba20388e60a23ba1564d363051d823b3df6aa`.
- Installed Textual: `dca5859a44bf77d5217f87ff8462796d632f083a`.
- Runtime: `/home/ts/.local/share/agent-comms/runtimes/core-ce3d3f889-lock-b1e0540c02f88324`.
- Actual user UI: PID 1038119, process birth 1793755098.96.
- Busy observation worker: PID 1038247, spawned directly by that UI.
- Other spawned Python workers 1038236 and 1038237 were nearly idle in the
  initial CPU measurement. ACP and Neovim children were also nearly idle.

These are historical process identities, not permission to signal whatever now
occupies those PIDs. Recheck identity before any later observation.

## Measurements and their limits

A 20-second psutil capture computed user+system CPU-time deltas per exact process
identity, divided by elapsed monotonic time. The UI averaged **25.9% of one core**;
worker 1038247 averaged **33.1%**. Other Python workers averaged approximately
0–0.1%. No inputs or UI actions were injected. User activity during that first
measurement was not recorded, so it is not an idle benchmark.

Next, Tristan was told when sampling started and explicitly reported
"scorlling". A final 30-second capture after that report sampled the UI and the
busy worker separately. This is a user-reported scrolling interval, not a
recorded key/wheel-event trace or input-to-paint measurement.

`py-spy 0.4.2 record --nonblocking` read stacks without pausing the processes,
at 60 Hz. Root permission was needed; the initial unprivileged attempt collected
no stacks and is excluded. No debugger, code injection, provider input or window
restart was used for profiling.

**Sampling quality matters:** the final UI capture recorded 528 samples and 427
sampling errors; the worker recorded 264 samples and 204 errors. The earlier
combined capture recorded 1,795 samples and 1,564 errors, with four invalid UTF-8
replacement characters when its JSON was decoded. Raw bytes are preserved.
Nonblocking stack races mean these are recurring hotspot leads, **not reliable
CPU-time percentages**. The separate OS CPU-time measurements above do not come
from those stack proportions. Do not turn sample weights into precise shares,
FPS, a 16 ms claim or proof of a particular invalidation cause.

### The later burst and the verification workload must be separated

During the later reported spike, a one-second OS sample showed the existing
Toad observer at approximately 112% of one core and the UI at 26%. Our separate
private retry-qualification guardian also consumed approximately 107% of one
core. That concurrent workload materially contributed to host load; it is not
proof that all of the spike came from Toad. The guardian was lowered to nice 19
and idle I/O priority. It subsequently timed out waiting for its copied owner to
start, before any input or frontend launch; cleanup retired its private root and
confirmed user settings unchanged. Lowering priority was load management, not a
product fix.

After that setup exited, a separate five-second CPU-time sample measured the
same UI at 2.2%, its observer at 5.4%, and runtime worker 1049324 at 0%. This
confirms variability, not resolution. These short samples cannot identify the
transition that caused the burst. Retain startup/owner-start work as a separate
investigation dimension from visible UI activity; do not equate a process label
or overlapping load with a source-level cause. The latest quiet sample is in
`after-private-setup-cpu.json` beside the existing evidence.

## Findings recovered from actual stacks

### 1. The busy worker is the observer, not primarily a Markdown renderer

Repeated stacks pass through:

```text
ObservationService.serve/run/observe
  ReadingObservation.step
    UnreadableHistory.outcome
      ThreadReading.read/presented
        history_views.read / ThreadPresentationBatch.read_sources
        notification_reads.apply
        message_notification.references/read_references
        NotificationAssignment.read/changed
          CoordinationStore.observing
          begin_snapshot
          store_files.execute / close
```

Starting sites at the observed revision:

- `src/agent_comms/ui_model/observation.py`: ReadingObservation.step around
  262–270; ThreadReading.read/located/presented around 912–957; service around
  1399–1447; notification-reading consumers around 594 and 848–863.
- `src/agent_comms/notification_assignment.py`: changed around 362–363;
  read around 523.
- `src/agent_comms/message_notification.py`: changed around 88–109;
  references/read_references around 224–237.
- `src/agent_comms/history_views.py`: presentation read around 161 and
  message_notifications_for_references around 222.
- `src/agent_comms/locked_store.py`: begin_snapshot around 858.
- `src/agent_comms/store_files.py`: execute around 508; close around 519.

The observed worker repeatedly performs database/snapshot and notification
projection work. **Not yet established:** which source revisions admit those
reads, whether unchanged evidence is being recomputed, query cardinality, and
whether any one query scales with retained history. A SQL leaf alone does not
prove that SQL is the root defect.

### 2. UI work is spread across layout, timers and refresh propagation

Actual UI stacks include:

- Textual timer `_run_timer → _run → _tick → _callback.invoke`.
- Textual compositor `run_steps`, layout, render_regions and message dispatch.
- `toad/screens/workspace.py` `_layout_steps` around 227–231:
  retains history readers, runs normal layout, places readers, and can run
  another scroll layout pass.
- `toad/widgets/history_anchor.py`: geometry targets and reader placement around
  948–959; page/for_layout around 355–361.
- `toad/widgets/conversation.py`: watch_scroll_y around 379–380;
  notification_targets around 233–236 scans compositor-visible widgets.
- `QueueEvidenceChanged → queue_observation_changed/queue_evidence_changed`
  around conversation.py 1685–1690, then
  `ConversationSubmission.publish_pending → Prompt.sync_queue` around
  conversation_submission.py 327 and widgets/prompt.py 722–730.
- `toad/transcript_state.py` execute around 429 and
  `transcript_source_preparation.py` _prepare_edge around 224.
- `toad/frame_presentation.py` animation_visible/render_line around 339–356;
  `toad/textual_colours.py` _tick/_display around 500–541.

Thread-pool stacks also show `mark_page_read → acknowledge → mark_channel_read`
and read-ledger/database work. This is another real consumer to include when
tracing notification revisions; it is not evidence that receipts alone explain
the scrolling slowdown.

**Not yet established:** whether reader placement causes redundant second
layouts, which timers remain active without visual change, and whether queue
evidence changes cause unnecessary prompt or whole-view invalidation.

## Continue from the owners, not a new cache or throttle

1. **Refresh sources and check overlap.** Read current AGENTS.md, project prompt
   when present, docs/DECISIONS.md and commander/NRA instructions. Fetch Core,
   Toad and Textual mains; inspect relevant open PR files before editing. Compare
   the exact observed heads above with current source instead of assuming the
   old line numbers still identify the same declarations. Reuse an existing
   isolated checkout per repository. Do not modify the dirty canonical checkout,
   paper work, native journals, auth or uncertain-input records.

2. **Map the observer's complete read/invalidation family with NRA/AST.** Trace
   every producer of the revision consumed by ReadingObservation and every
   notification/queue/read-marker consumer. Establish what changed at the source,
   what answer each view actually needs, and how its existing owner can derive
   the affected set. Read the complete snapshot/query acquisition paths and
   their cardinalities. Identify repeated reads of unchanged evidence or a
   history-sized query only when the declaration-to-consumer trace establishes
   it. Preserve source recovery and all notification/delivery distinctions.

3. **Trace UI invalidation through the existing frame/layout owners.** Map
   scroll destination intent, reader anchors, geometry publication, timer
   callbacks, QueueEvidenceChanged and prompt queue summaries. Find the exact
   state transition that requires another layout or repaint; distinguish that
   from repeated propagation of an unchanged value. Preserve warm bodies,
   reader position, selection, drafts/caret, Vim mode and live arrival behavior.

4. **Implement one coherent owner repair per demonstrated cause.** Move shared
   behavior into existing owning classes/capabilities and migrate all consumers;
   delete replaced paths. No second polling loop, timer, mutable cache,
   invalidation registry, throttling workaround, scan ledger or per-widget copy
   of notification truth. Do not treat a faster example or fewer source lines
   as evidence that the required answer remains complete.

5. **Confirm the burst cause and its removal, not just smoother scrolling.**
   Compare active and settled periods, background arrivals, tab return, and
   managed-owner startup separately. Capture the source transition admitting
   expensive work and whether the same unchanged state makes it run again.
   Account for every participating UI/observer/runtime process and unrelated
   qualification work; do not hide the cost by moving it to another worker.
   Show that work settles when its source settles and stays bounded by the
   changed/shown data rather than lifetime history. Include a burst-capable
   workload and retain before/after evidence of the demonstrated cause.

   Focused behavioral
   checks follow the coherent source change, not repeated full suites or local
   ratchets. Compare the same saved-state scrolling journey before/after,
   measuring UI and observer CPU separately, and preserve the workload and
   source heads. Include short and long saved histories when testing bounded
   cost. Record profiler errors and input timing honestly. A cloud environment
   without the installed native/UI path must state that limit and leave its
   exact source checkpoint for host qualification, not claim a local fake is
   installed acceptance. Follow the repository's one-fresh-DM copied-agent
   runtime qualification rule for an installed repair; never replay an old or
   uncertain input.

## Evidence on ts (not required to understand this handoff)

Retained under `/home/ts/.cache/agent-scratch/codex-latency/toad-cpu/`:

- `processes.json`, `cpu-times.json` — actual process relationships and CPU deltas.
- `confirmed-scroll-ui.speedscope.json`, `confirmed-scroll-observer.speedscope.json`
  and their `.log` files — final capture after the user's scrolling report.
- `confirmed-summary.json` — derived frame summaries, with sample weights,
  not CPU percentages.
- `scroll.speedscope.json`, `scroll-profiler.log`, `scroll-summary.json` — initial
  combined evidence; its quality limits are stated above.
- `scroll-ui.*`, `scroll-observer.*` — preceding separate capture.
- `py-spy*.log` — failed unprivileged attempt and successful read-only root dump.

These files are local; a cloud agent should start from the findings and exact
source chains in this committed plan. Keep raw profiles while a real comparison
needs them, then retire redundant captures under the existing cleanup rule.
