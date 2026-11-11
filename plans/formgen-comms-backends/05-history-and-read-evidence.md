# 5. Conversations, history and actual painted reads

**Planning status:** page/proof ownership, acknowledgement order, source retirement and the caller set below are traced. The exact shared fragment bases, pixel-versus-row position contract and Qt paint admission still need resolution. This phase is not ready to execute as a whole.

## Outcome

Both backends use the original conversation, page, source-state, coverage and read-acknowledgement algorithms. Native views render those messages and report their actually painted body content. Loading, laying out or selecting a message does not mark it read.

The retained source is Toad `cdc6a761b476bd0b25bd323d88860cf5f7d56fe4`; a fresh fetch during this pass returned the same `origin/main`. Core paths below refer to the source read recorded in [sources.md](sources.md), not an instruction to freeze it.

## 5.1 Keep Core's page and proof

Core `agent_comms/messaging/message_page.py:30–68` already owns these answers:

- `MessagePage.messages`, traversal flags and `selection` are the original response.
- `message_evidence()` associates each message with its original source page. It builds an index once; do not rescan every source page for every message.
- `select(sequences)` derives the subset through the original selection. It is not a new proof assembled from a viewer name and sequence numbers.
- `same_display` compares the original selection identities. A copied UI identity is not that check.
- `combine` keeps `source_pages`; a combined page does not replace every message's original proof with its newest page's proof.

Keep these declarations in Core. The client uses `CommsConversationPageTool` for the original typed conversation and `CommsMarkPageReadTool` for the selected original page. Qt must not write the read ledger, query a phone-local copy of the server bus or manufacture a new display basis.

Toad `ConversationKind.seen_page` at `conversation_kind.py:135–142` groups pending fragments by **page object identity** before calling `page.select`. Keep that original body on the shared conversation owner selected in phase 3. Equality of pages is not permission to merge their pending acknowledgement lifetimes.

## 5.2 Original owners and destinations

The destinations are proposed modules inside the existing Toad distribution. This table is not a claim that their current imports are already native-free.

| Original declaration | Shared owner/destination | Native remainder |
|---|---|---|
| `WireCut`, `BeforeMessage`, `AfterMessage`, `AfterSequence`, `NothingRead`, `WirePage` | `toad/client/history.py` | None of the cursor/acceptance algorithm becomes a Qt row index |
| `WireSource`, `WireObservation`, `ReplacedWireObservation`, `GrowingWireObservation` | Same shared history module | Fragment preparation/render construction uses the selected native family; original Core access, retained conversation, source identity and observation decisions remain shared |
| `WirePageBuffer` and `ChannelDestination` | Shared history/submission owners | View access and scheduling use the shared host/session contract, not `query_ancestor` or a concrete screen |
| `HistoryCoverage`, `NoCoverage`, `PageCoverage`, `CoveredHistory`, `ReportsCoveredPages` | `toad/client/history_coverage.py` | `AbstractHooksMeta` is replaced with the actual shared ABC contract; native document acquisition/painting remains native |
| `HistorySourceSnapshot`, source work and retirement algorithms in `TranscriptSourcePreparation` | `toad/client/history_preparation.py` | Textual frame defer, DOM lookup, Worker cancellation/join and AwaitComplete construction |
| Original `TranscriptState` and its working, parked, retired and request members | `toad/client/history_state.py` | Original native work handles become the phase-2 operation contract; no parallel state flag/table |
| Pending-painted map and read acknowledgement methods in `widgets/wire_history.py:172–237` | Shared `ReadMarkedHistory` under `toad/client/history_reads.py` | Attachment/current-window answers, native completion scheduling and actual body-paint reporting |
| `PaintedRead`, `NothingToRead`, `ReadWork`, `VisibleReadMarking` | Shared read-work meaning and single-operation lifetime | Textual Worker creation stays native; Qt starts the same work through its persistent runtime |
| `HistoryNotifications`, `SourceNotifications` | Shared notification subscription/application behavior under `toad/client/history_notifications.py` | Native show/hide/mount/unmount, row redraw and callback scheduling |
| `PaintedTranscriptRow`, `RenderedTranscriptRows`, `ReadTranscriptPage` | Remain Textual-native | `Region`, `Offset`, `Strip`, body-row detection and Textual output admission are not shared pixel geometry |

Do not move `wire_history.py` or `widgets/transcript_history.py` wholesale. They currently mix source/read meaning with LineBlock, Lines, native fragments, screen lookup and widget lifetime. A module move that preserves those dependencies does not supply a shared application.

## 5.3 Share the acknowledgement body, not just an interface

Move the existing `WireHistory` pending-evidence methods as one coherent family change. The shared parent owns the pending map and these original bodies: `retain_painted`, `retire_painted`, `read_fragments`, `read_visible`, `_mark_read`, `_mark_page` and `reload_notification_source`. Remove those bodies from the Textual member in the same change. Do not keep two maps, one in a logical history and another in its native widget.

Required behavior from the actual source:

1. `retain_painted` accepts a fragment carrying its original page evidence. A new fragment object with the same key can replace an old object. Schedule acknowledgement only after native paint admission.
2. `read_fragments` returns nothing from a detached/non-drawing view, but valid earlier painted fragments remain retained while scrolling or page eviction changes layout.
3. `read_visible` starts nothing if there is no attached evidence to submit. The single-operation owner does not start a second acknowledgement while its current one runs.
4. `_mark_read` captures a `HistorySourceSnapshot`, checks both source identity and the current route, and rechecks after the awaited route read. Being hidden does not by itself invalidate acquired source I/O; permission to draw is a separate answer.
5. Select one original proof's fragments through `ConversationKind.seen_page`, submit that page through `WirePageBuffer.mark_page`, then retire exactly the acknowledged fragment objects carrying that proof.
6. On `ReadAgain`, retire all queued fragments carrying the refused proof, including ones outside the selected subset. Keep unrelated proofs and a newer fragment already painted under the same key. Reload only if that source snapshot remains current.
7. A later native refresh paints replacement evidence before trying that message again. Never resubmit the same refused proof in a retry loop.
8. Maintenance refusal leaves the message unread; it does not forge success, erase unrelated evidence or replay a send.

The current native base list is `WireHistory(VisibleReadMarking, ReadMarkedHistory, ConversationHistory[ViewT])`. `ReadMarkedHistory` currently inherits native `LineHistory`, so moving its import alone leaves Textual in the shared contract. The extraction must separate that dependency before both native histories inherit the shared methods.

**Remaining declaration work:** list the exact common fragment base, its original message/key/evidence fields, native Textual and Qt fragment bases, dataclass initialization order and read-host signatures. `WireFragment` currently inherits `TranscriptFragment` and contains both evidence and LineBlock rendering; `MessageFragment` additionally crosses `BusMessageFragment`. Preserve those role/notification meanings without a second message record or a dict keyed by kind. No native-free fragment hierarchy is claimed yet.

## 5.4 Actual paint stays native

### Existing Textual path

`ReadTranscriptPage.capture_render` at `widgets/wire_history.py:61–72` captures the **original strips and fragment body rows** for that render. `RenderedTranscriptRows.admit` at `:51–55` accepts an exposed intersection only if its cropped strip contains nonblank text. Then `WireHistory.retain_painted` keeps the original fragment and proof.

Do not replace this with `visible_rows` computed after a resize. A pending layout can differ while the previously accepted frame remains on screen. Header, divider, fully covered body and blank body cells do not prove that message text was shown. Partial exposed body text does.

Keep `PaintedTranscriptRow` and the accepted publication immutable with respect to later fragment layout. Its native region is evidence about the accepted output, not a request to query the widget again.

### Required Qt path

Qt must report the original fragment/proof from the body drawing operation and its native exposed/clipped content. Merely receiving a `QPaintEvent`, finding an item within a scroll rectangle, completing model insertion, or calling `processEvents` is insufficient. Header-only and overlay-covered cases must remain unread.

The exact native page/view class, paint callback, clipping/overlay admission and completion hook are **not yet selected and checked**. Finish that source/native investigation before calling phase 5 executable. The hook must capture the rendered body and original proof before later layout mutates them, then enter the shared acknowledgement owner. It must not maintain a second read ledger or translate a terminal row count into pixels.

Reader anchoring likewise keeps logical fragment identity separate from native geometry. The exact generic position contract must be resolved against `HistoryWindow`, `ReaderPosition`, fragment locations and Qt scrolling; sharing their current terminal units unchanged is not a plan. Check the retained content/fragment position and page origins as well as scroll_y: layout compensation can legitimately change the native absolute offset. A test that aborts on an offset before checking painted rows has not yet established reader drift; do not port a guessed one-row correction into Qt.

## 5.5 Source work, parking and shutdown

Preserve the original `TranscriptSourcePreparation` order at `transcript_source_preparation.py:431–489`:

- A superseded operation cannot publish or finish a newer operation. Completion checks the held operation object, not only a global generation count.
- Retirement first closes its observation interest, chooses the existing parked/retired state, increments generation and removes drawing participation.
- Preparation ownership retires through that state. A parked document's preparation remains with its logical owner rather than being destroyed merely because its widget unmounts.
- Cancel the operation's native handles and join those exact handles independently. The current Textual join deliberately starts in a fresh Context so it cannot wait on itself.
- Closing a reader and releasing retained pages are distinct operations. Notification interests stop when the view leaves; earlier acquired work can finish without permission to publish to a replacement view.
- Resume uses the existing state transition, reattaches the original document/window, then reports coverage/read eligibility and requests preparation.

The shared methods return ordinary awaitables/operation results. Textual retains its AwaitComplete/Worker adaptation; Qt retains actual task/callback ownership and native destruction. Phase 2 decides their common lifetime, rather than duplicating this sequence in a Qt close handler.

Coverage remains `HistoryCoverage`, not a second boolean on the screen. Native transcript coverage, committed sent reports and painted bus-message read evidence answer different questions and must not be merged.

## 5.6 Callers and import changes

The current import search covered `src`, `tests` and `tools`. The direct caller files below are a concrete move surface; finish binding/dynamic-entry checks before claiming the final complete graph.

Production: `wire_conversation_view.py`, `transcript_publication.py`, `frame_presentation.py`, `block_navigation.py`, `transcript_preparation.py`, `transcript_source_preparation.py`, `transcript_filter.py`, `transcript_state.py`; screens `historical_sessions.py`, `session_view.py`, `main.py`; widgets `transcript_history.py`, `history_anchor.py`, `committed_presentation.py`, `presentation_window.py`, `outgoing_message.py`, `comms_chat.py`, `transcript_lines.py`, `conversation.py`, `wire_history.py`, `transcript_fragments.py`; and the original `wire_history.py`/`read_marked_history.py` owners themselves.

Tests/tools: `tests/native/test_native_journey.py`, `test_native_editor_queue.py`; `tests/support/textual_steps.py`; `tests/guards/test_deleted_interface_mechanisms.py`; `tools/performance/frame_meter.py`; the Textual test files listed below plus `test_arrival_fade.py`, `test_scroll_down_bounce.py`, `test_stale_route_writes.py`, `test_conversation_kind.py`, `test_message_filter_restyle.py`, `test_session_tall_arrival.py`, `test_wire_message_lines.py`, `test_window_top_edge.py`.

Migrate moved logical imports to their shared owners. Native modules import their actual native renderer. Remove former bindings rather than re-exporting them. Guarded/local imports in touched modules need NRA's import-graph operation or a declaration move that resolves the cycle; do not mechanically lift them without its proof.

## 5.7 Ordered work and checks

1. Finish the remaining fragment, geometry and native-paint decisions above; record concrete bases/method winners and signatures. Do not start a Qt-only transcript application while those contracts remain unresolved.
2. Move pure cursor/coverage declarations with binding-removing NRA relocation. Migrate the listed consumers in that same change.
3. Extract the original source/read/notification bodies to the selected shared parents; both native members inherit them. Use actual member-promotion operations only within their proven ancestor scope. The existing full form scan does not certify these history moves.
4. Retain native Textual render/paint/geometry bodies. Supply Qt's irreducible native view, paint, layout, input and close operations; delete all copied source/read algorithms.
5. Run import isolation, actual MRO/constructor checks, original behavior tests and native controls. A source parse or proposed hierarchy does not establish native paint admission.

Existing required tests include `tests/textual/test_read_held_frame.py`, `test_divider_only_viewport_stays_unread.py`, `test_painted_read_retry.py`, `test_painted_read_maintenance.py`, `test_saved_history_coverage.py`, `test_wire_source.py`, `test_message_notification_title.py`, `test_channel_scrolling.py`, `test_app_close_order.py`, plus the changed-name consumers above. `test_painted_read_recovers_when_scope_changes_before_ack` delays the real acknowledgement, changes channel scope and checks that the refused proof is submitted only once before fresh evidence succeeds. Keep that real Core path; do not replace it with a private-field fixture.

Required Qt controls: real body/partial body/header-only/covered-body/blank-body painting; held output plus pending reflow; source replacement during acknowledgement; scope change and ReadAgain; hide/park/close during source work; new paint under the same key before an older reply returns; maintenance refusal; read during a connector outage and recovery. Sends with unknown outcomes remain unknown and are never replayed by any of these controls.

Consumer runs exercise actual body painting and the original Core acknowledgement path in both backends, including stale proofs, later paint under the same key and source replacement.
