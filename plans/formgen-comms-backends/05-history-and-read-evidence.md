# 5. Conversations, history and actual painted reads

**Planning status:** page/proof ownership, acknowledgement order, source retirement and the caller set are traced. §5.8 decides the three remaining declarations, read against Toad main `1421d01d4`:
- **Fragment base:** every member of the five fragment classes, classified shared or native.
- **Position contract.**
- **Qt paint admission:** on `QAbstractScrollArea` viewport painting.

There is no `toad/qt/` on main yet, so the Qt view is chosen here, not found. Its admission rules must be proven by the native controls in §5.7 before the backend is reported working; that is execution, not open design.

## Outcome

Both backends use the original conversation, page, source-state, coverage and read-acknowledgement algorithms. Native views render those messages and report their actually painted body content. Loading, laying out or selecting a message does not mark it read.

The initial retained source was Toad `cdc6a761b476bd0b25bd323d88860cf5f7d56fe4`. The member corrections below read immutable current-main objects at `61557554987d7ddb36f67d27380ee3e0a043f64a`; these history owners are unchanged from reviewed `1421d01d4`. Core paths below refer to the source read recorded in [sources.md](sources.md), not an instruction to freeze it.

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

The fragment base, its fields, the native Textual and Qt bases, the dataclass initialization order and the read-host signatures are declared in §5.8. `WireFragment` currently inherits `TranscriptFragment` and holds both evidence and LineBlock rendering; `MessageFragment` also crosses `BusMessageFragment`. §5.8 keeps those role and notification meanings, with no second message record and no dict keyed by kind.

## 5.4 Actual paint stays native

### Existing Textual path

`ReadTranscriptPage.capture_render` at `widgets/wire_history.py:61–72` captures the **original strips and fragment body rows** for that render. `RenderedTranscriptRows.admit` at `:51–55` accepts an exposed intersection only if its cropped strip contains nonblank text. Then `WireHistory.retain_painted` keeps the original fragment and proof.

Do not replace this with `visible_rows` computed after a resize. A pending layout can differ while the previously accepted frame remains on screen. Header, divider, fully covered body and blank body cells do not prove that message text was shown. Partial exposed body text does.

Keep `PaintedTranscriptRow` and the accepted publication immutable with respect to later fragment layout. Its native region is evidence about the accepted output, not a request to query the widget again.

### Required Qt path

Qt must report the original fragment/proof from the body drawing operation and its native exposed/clipped content. Merely receiving a `QPaintEvent`, finding an item within a scroll rectangle, completing model insertion, or calling `processEvents` is insufficient. Header-only and overlay-covered cases must remain unread.

The native view class, paint callback, clipping and overlay admission, and completion hook are selected in §5.8. They are checked by the §5.7 native controls when the view is built. The hook must capture the rendered body and original proof before later layout mutates them, then enter the shared acknowledgement owner. It must not maintain a second read ledger or translate a terminal row count into pixels.

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

1. Implement the selected fragment, position and native-paint contracts in §5.8, recording actual bases, method winners and constructor signatures. The decisions below are the plan; native execution and per-site NRA proof/refusal remain implementation work. Do not start a second Qt-only transcript application.
2. Move pure cursor/coverage declarations with binding-removing NRA relocation. Migrate the listed consumers in that same change.
3. Extract the original source/read/notification bodies to the selected shared parents; both native members inherit them. Use actual member-promotion operations only within their proven ancestor scope. The existing full form scan does not certify these history moves.
4. Retain native Textual render/paint/geometry bodies. Supply Qt's irreducible native view, paint, layout, input and close operations; delete all copied source/read algorithms.
5. Run import isolation, actual MRO/constructor checks, original behavior tests and native controls. A source parse or proposed hierarchy does not establish native paint admission.

Existing required tests include `tests/textual/test_read_held_frame.py`, `test_divider_only_viewport_stays_unread.py`, `test_painted_read_retry.py`, `test_painted_read_maintenance.py`, `test_saved_history_coverage.py`, `test_wire_source.py`, `test_message_notification_title.py`, `test_channel_scrolling.py`, `test_app_close_order.py`, plus the changed-name consumers above. `test_painted_read_recovers_when_scope_changes_before_ack` delays the real acknowledgement, changes channel scope and checks that the refused proof is submitted only once before fresh evidence succeeds. Keep that real Core path; do not replace it with a private-field fixture.

Required Qt controls: real body/partial body/header-only/covered-body/blank-body painting; held output plus pending reflow; source replacement during acknowledgement; scope change and ReadAgain; hide/park/close during source work; new paint under the same key before an older reply returns; maintenance refusal; read during a connector outage and recovery. Sends with unknown outcomes remain unknown and are never replayed by any of these controls.

## 5.8 The three remaining declarations

Read on Toad main `1421d01d4`.

### Fragment family

All names introduced below are selected implementation targets. The original native objects and field values remain their own owners; this documentation does not claim their new MRO or constructors have run.

Every member of `CursorRest` (`widgets/transcript_fragments.py:387`), `TranscriptFragment` (:431), `BusMessageFragment` (:662), `WireFragment` (`wire_history.py:221`) and `MessageFragment` (:316) was classified by whether its body names a native type. That means a Textual or Rich type, `LineHistory`, `LineBlock`, `Lines`, row acquisition, `HistoryWindow` or a cursor block.

The original native dependencies decide the split; the former claim that all BusMessageFragment members were native-free was wrong. `sticky_header` (:496) returns Divider objects, `request_show` (:526) changes RowAcquisition owners, `retained_bytes` (:627) counts their preparations, and BusMessageFragment carries a native MessageRow (:667).

| Shared (`toad/client/fragments.py`) | Native (original Textual bodies; distinct Qt presentation bodies) |
|---|---|
| **`CursorRest`:** original empty/default `rested`, `clipboard_text`, `expandable`, `expanded` | **`TextualCursorRest`:** `drawn_in`, `selects`, `placement_in`, `show_in`, `show_head`, and native MenuCommand-returning `block_menu` |
| **`HistoryFragment(CursorRest)`**: `events`, `part_position`, `starts_agent_activity`, `shown`, `has_details`; `source_identity`, `key`, `arrival_position`, `shown_live_by`, `notification_references`, `notification_identity`, default `queue_entries`, `draws`, `text_parts`, `outgoing_sources`, `capture_part`, `categories`, `timestamp`, source `clipboard_text`, `rested`, `expandable`, `expanded`, `shown_in` | **`TextualFragmentRows`:** `_lines`, `_plain`, `details_link`/FragmentDetails, `text_rows`, `sticky_header`, `consumed`, `blocks`, `line_blocks`, **`request_show`**, `link_place`, `lines_for`, `retains_lines`, `invalidate_lines`, `row_change`, `hold_lines`, `stand_in_lines`, **`retained_bytes`**. Cursor drawing/select/placement/show bodies stay on TextualCursorRest |
| **`BusMessageHistoryFragment(HistoryFragment)`**: `text_part`, original Core `notification_identity`; `capture_part`, `text_parts` including the empty-message case, `has_details`, `notification_references`, `queue_entries` after the notification split below, `draws`, `shown_live_by`, `body_text` | **`TextualBusMessageRows`:** native `row: MessageRow` and the MessageShown default; `block_menu`, style-dependent `clipboard_text`, `leading`, `sticky_header`, `text`, `trailing`, `blocks` (:699–727) |
| **`EvidencedFragment(HistoryFragment, ABC)`**: original `evidence`, `checks_notifications`, role-factory algorithm, base `from_message`, `message`, `timestamp`, `clipboard_text` | **`TextualWireRows`:** native MessageShown field specialization and original `leading`, `trailing`, abstract `text`, `blocks`, `text_rows`. Keep MembershipFragment.text, HistoryLeading and MessageFragment.leading native |
| **Message occurrence/notification meaning** | **MessageFragment.from_message** (:320) stays native because it constructs MessageRow; move the pure original `notification_references` override to shared `MessageNotificationReferences`, inherited by both native message classes, while retaining BusMessageFragment/WireFragment precedence for native rows. Qt construction supplies its own actual row over the same Message and MessagePage, not a copied message record |

Resolve the dependencies beneath those fields before promotion:

- `EventSourceKey` and `FragmentKey` move unchanged to the shared module. `FragmentDetails(ToggleDetails)` stays native; a shared key is not a native link object.
- Extract **`FragmentShown`** from Shown's `details_open` field and `toggled` body. Keep native `Shown(FragmentShown).summary` (:238), which builds Summary/Span/LineRole/LineLink. **MessageShown remains native**: it inherits that native summary and owns WireMessageStyle plus the original status. Qt has a native shown member inheriting the same disclosure-state base. No shared import of MessageShown, Shown.summary or WireMessageStyle is permitted.
- The shared fragment's shown type is the pure disclosure-state contract; a non-dataclass nominal message-state contract adds the `status` annotation. The native MessageShown dataclass keeps its original `style`, `status` declaration/default order. There is one shown value on the fragment, never a synchronized second disclosure/status record.
- `PartPosition` is also mixed: extract its index/has-details/notification-reference/outgoing-source/start-activity contract to shared **`FragmentPart`**. Keep native `draw_divider`, `leading` and `status_blocks` on the original PartPosition/LaterPart/LastPart/InnerPart presentation members. Move the original `sequence` case-selection algorithm once to FragmentPart. The pure OnlyPart/HeadInnerPart/LaterInnerPart/LaterLastPart meanings retain their original crossing LastPart/InnerPart/LaterPart behavior; the actual native part root inherits DeclaredViews and selects its presentation member for each meaning. Textual and Qt declare their native part members under those roots. The one shared sequence body selects through that actual root, as phase 1's editor selector does; there is no second per-backend sequence algorithm or hand-kept case roster. The Textual field's original OnlyPart default becomes the native root's view of that same OnlyPart meaning. Shared fragment methods access only the declared pure contract, never a native default factory.
- Split `NotificationStatus` at `notification_status.py:44` before moving `queue_entries`: its original NotificationStatus, UncheckedNotifications, CheckedNotifications, EmptyCheckedNotifications and UnavailableNotifications state/data, `failed`, working/unavailable, titles, recipient identities and checked-result selection become the shared notification family. Span/LineRole details, `disclosure`, `role`, `blocks` and `recipient_line` remain the native notification projection. The original MessageQueueEntry `reference`/`status` declaration and string summary become shared; its Span-returning details are native presentation. Native projections consume that same status and entry, not another notification cache. Select `TextualNotificationPresentation(MroProjection)` for the original role/details/queue-details/blocks rendering, with native handlers over the original shared status family; Qt supplies its native projection over those same members. `PartPosition.status_blocks` calls that native projection rather than a deleted `status.blocks`; native queue disclosure uses its entry/status projection rather than importing Span-returning details into the shared entry. All NotificationStatus/MessageQueueEntry data imports move to the shared owner while native rendering imports remain native. Thus the shared queue-entry construction still uses the original references/status without importing line blocks.
- **AgentActivityPresentation remains native** at `widgets/agent_activity.py:10`: `group`, `thinking` and `tool` build Summary, MarkdownMessageBody, Body and LineBlock. Move only `tool_has_details`'s pure title/detail predicate to a shared activity-meaning capability, taking `title: str` plus the original `details_available` boolean rather than importing native ToolCallStatus. Retain the original splitlines/boolean body. Update its two native callers, `widgets/tool_call.py:220` and `widgets/transcript_fragments.py:647`, to pass their original status.title; the native presentation inherits the one predicate. `consumed`, FragmentLineConsumer and ToolTranscriptFragment.blocks remain native; Qt must build its own native activity rows, not reuse Textual line blocks.

The selected Textual composition is:

```python
class TranscriptFragment(HistoryFragment, TextualCursorRest, TextualFragmentRows): ...
class BusMessageFragment(BusMessageHistoryFragment, TextualBusMessageRows, TranscriptFragment): ...
class WireFragment(EvidencedFragment, TextualWireRows, TranscriptFragment, ABC): ...
class MessageFragment(MessageNotificationReferences, BusMessageFragment, WireFragment, roles=(ConversationRole, NoticeRole)): ...
```

The corresponding Qt classes substitute QtCursorRest, QtFragmentRows, QtBusMessageRows and QtWireRows, retaining the same shared identity/evidence providers. Preserve MessageFragment's own native construction/leading overrides and the original Bus-before-Wire lookup winners for clipboard, blocks, text and trailing. Compare every resolved owner, not just the printed base list, during implementation.

The factory's original role lookup/error/registration body is shared once, but each actual native WireFragment root owns its own role registry. Replace the original hardwired WireFragment registry receiver with that selected root; do not register both native MessageFragment classes in one global role table where import order chooses the renderer. The original `roles` keyword, role ordering and unsupported-role error remain. This is a planned scoped-factory change, not a claim that a current codemod automatically proves it.

**Construction:** retain positional `events`, then `starts_agent_activity`; part_position and shown remain keyword-only, with their native OnlyPart/Shown/MessageShown defaults supplied at the native declarations. Shared annotations name FragmentPart/FragmentShown, not native defaults. `_lines` and `_plain` remain init=False and native. Bus text_part/row and Wire evidence retain their original keyword-only contracts. Constructor-field/default/MRO checks must cover TranscriptFragment, BusMessageFragment, WireFragment and MessageFragment plus both native factory roots; no new argument bag or re-export is introduced.

`NO_REST` remains the actual no-drawn-rest value of each native cursor family. Move the shared default algorithms without manufacturing a shared native placement or menu sentinel; Textual's original consumers receive its TextualCursorRest value, and Qt receives its own.

### Read host

`ReadMarkedHistory` (`read_marked_history.py:9`) inherits `LineHistory` only to sit in the native history's MRO. Its one subclass is `WireHistory(VisibleReadMarking, ReadMarkedHistory, ConversationHistory[ViewT])`, which already gets `LineHistory` through `ConversationHistory`. So the shared `ReadMarkedHistory(ABC)` drops that base, and `WireHistory`'s resolved members are unchanged.

`ReadMarkedHistory(ABC)` declares the read-host contract:
- `page_source: ReplaceablePageSource`
- `undrawn` (property)
- `async source_is_current(snapshot: HistorySourceSnapshot) -> bool`
- Concrete `async _mark_read(snapshot: HistorySourceSnapshot) -> None`, moved from WireHistory; remove the former abstract stub before promoting this body.

It takes the §5.3 bodies (`retain_painted`, `retire_painted`, `read_fragments`, `read_visible`, `_mark_read`, `_mark_page`, `reload_notification_source`) and the one pending map. The `ReadMarkedHistory` annotations in `wire_history.py:423, 525, 528` keep working unchanged, because they name the contract and not `LineHistory`.

### Position contract

`history_anchor.py:169–653` contains a complete position family, not just a key plus an opaque distance. The shared family moves to `toad/client/reader_position.py`, parameterized by the native **distance, item, page and retained-presentation types**. Native objects are retained as those generic values; they are not replaced with keys or wrapper copies. Source-addressed and native-holder positions keep their distinct lifetimes.

These are selected new contracts, not existing APIs. **ReaderDistance** supplies same-unit addition/subtraction, ordering, zero and `fraction_of(extent) -> float`; its separate nominal members are TerminalRows and ViewPixels. Their arithmetic implementation can be inherited, but their identities cannot be merged. **ReaderWindow** declares the existing following/history-change/source answers and the native effects below. Each native window captures its geometry in its own distance type. There is no terminal-row/pixel conversion.

| Original declaration/members | Shared behavior and data retained | Native boundary |
|---|---|---|
| **ReaderPosition** (:169–283) | `ranges`; `of`, empty `required_presentations`/`bodies`, `for_layout`, `laid_out`, `moved`, `left_tail`, `parked`, `joined_tail`, `arrived`, `revealed`, `placed`, `without_holder`, preparation methods, source/close hooks and `placed_by_layout`; abstract `place`/`shifted` | ReaderWindow supplies typed top-page, placement, original history-change and preparation owners, captured ranges and place-reader effects. Replace both explicit motion zeros in `placed`/`without_holder` with this window's distance zero, not a unitless integer |
| **TailPosition** (:288–316) | Original earlier-page preparation, tail-following transitions, unchanged shift, and arrival choice | `anchor_reader`, native scroll/tail offsets and a layout-scroll context implement the original anchor/write order; revealed rows use the same context |
| **OffsetPosition**, **RowPosition** (:319–344) | Original destination/place/reached comparison and RowPosition.y | `place` releases the tail anchor before setting the destination. **`shifted` retains `replace(self, y=self.y + distance)`**, using ReaderDistance addition. A stored RowPosition offset stays native-unit geometry, not a message key |
| **FragmentPosition** (:348–386) | Original key, fraction, captured height, keyword-only block index; page search, bodies, for-layout, measured check, preparation start, destination and shift | Shared FragmentLocation/ReaderPage contracts expose the original occurrence and native items. **`shifted` retains `fraction + distance.fraction_of(captured_height)`**. Captured height is in the same native unit as motion; the fraction is dimensionless. Do not omit this division or recompute height from later layout |
| **UnshownReader**, **UNSHOWN_READER** (:389–402) | Original unplaced/no-motion/no-layout result | No native object is fabricated |
| **NativeReaderHolder** (:405–409) | Original `required_presentations` returns the actual held presentation, preserving identity | Type parameter is the actual native presentation, not a source key or a second document |
| **BlockPosition** (:413–447) | Original block/item, captured row and y; choose the minimum placed candidate, preserve holder/placement decisions; destination remains **current item position + captured y - captured row** | Window supplies actually published, placed, non-container live candidates; Textual retains published_widgets/ancestors filtering at this boundary, Qt uses its admitted native output. `holder_current(item)` implements original attachment plus window membership. `bodies` returns the held native item, not its fragment key |
| **ArrivalPosition** (:451–500) | Actual page and key; location/bodies/for-layout/measured/destination/tall/place/revealed/unchanged-shift bodies, including tall-message head versus ordinary tail choice | ReaderPage/FragmentLocation supply original span and same-unit drawn height; native window supplies membership and viewport-height comparison. Keep the original page even if a fresh fragment with the same key appears elsewhere |
| **Settlement**, **Unsettled**, **Scheduled**, **Ready**, **HoldProgress**, **HeldReaderDestination** (:504–561) | One mutable progress owner, same held-position identity check, source-settlement scheduling, reached/ready/measured gate and released/release behavior | `after_source_presented(callback)` replaces call_after_refresh at the existing native boundary; `request_reader_layout()` replaces refresh(layout=True). Textual retains its actual refresh order; Qt waits for admitted native source paint, not a guessed timer |
| **ParkedPosition** (:565–604) | Same held position/ranges; original required-presentations, prepare, bodies, layout, place and shifted delegation; parked/without-holder/released answers | Delegation retains native objects only when the held position originally required them. Source-key positions can reprepare fresh fragments; native holders retain the original presentation |
| **LatestPosition** (:608–653) | Original NativeReaderHolder/HeldReaderDestination/TailPosition precedence; revision, exact `(source preparation, through)` tuples, awaits-source-advance, hold/current/restore/source-settled/source-arrived/release/arrived/released | Window supplies anchor, scroll revision, current-position assignment, follow-latest and place-reader operations. Preserve LatestViewportRequest and the original source objects; neither source advance nor currentness becomes a new flag beside them |

**Native declarations:** Textual HistoryWindow implements ReaderWindow using its current layout/publication/scroll owners; its current page and FragmentLocation implementations supply ReaderPage/FragmentLocation. QtHistoryWindow, QtHistoryPage and its drawn/not-loaded locations implement those same contracts over pixels and actual Qt items. Keep TopPage/PageBelowTop/PageUnderTop and PinnedHeader native: they measure native top rows/columns and construct Divider headers. A native top-page capture constructs the shared FragmentPosition with the original key, dimensionless fraction and native captured height; a live-block capture supplies the original item, row and y. Geometry is decoded once at these capture boundaries, not probed again in shared code.

Keep constructor names, field order and keyword-only block/ranges semantics. The intentional annotation changes are float/int geometry to the window's distance type and Widget/page/presentation to generic native type parameters. Update every capture site in HistoryWindow, top-page/location owners and native parked/restore consumers together. A source key is never substituted for BlockPosition.block, ArrivalPosition.page, a required presentation or LatestPosition's source-preparation object.

Preparation keeps the original OLDER_END/NEWER_END walk algorithms. Shared position methods obtain those direction capabilities from the shared preparation host rather than importing native `widgets/presentation_window.py`; move their pure traversal provider with the phase-5 preparation family and leave native placement/measurement effects on that host. The `rows_needed` motion/extent inputs become that window's distance type; fragment/page range indices and source positions remain integers/original keys. NativePresentation stays an actual native resource, never a second logical preparation store.

The shared `ReaderPosition`/`OffsetPosition` factories select the same case meanings for both backends; backend selection is in the native window, not a kind string. Native positions cannot cross to another renderer's window. Required implementation controls exercise same-unit addition, captured-height fraction division, zero motion, unplaced holders, reflow/eviction, tall arrival, park/restore, End/source settlement and retained-presentation identity. This table accounts for the arithmetic and native holders the former store-and-hand-back description omitted.

### Qt paint admission

The Qt history is a `QAbstractScrollArea` that paints its own viewport. It is not a `QListView` or another item view: an item view paints items through delegates with no per-glyph exposure report, so it cannot tell a header-only row from a painted body.

Its `paintEvent(event)` paints the fragments its layout places inside `event.rect()`, in z-order. For each fragment it records which **body** text runs were drawn with non-blank glyphs inside the region left visible after overlay widgets (the sticky header, the jump-to-latest control). It uses the painter's clip region minus each overlay's mapped geometry.

When the paint finishes, its immutable native frame admission holds the original fragment objects, their `evidence`, and the admitted body runs. It calls shared `ReadMarkedHistory.retain_painted(fragment)` once for each admitted original fragment, matching Textual's actual call. Do not call this frame data `PaintedRead`: the existing PaintedRead/NothingToRead/ReadWork family at `widgets/message_notifications.py:117–143` owns whether/how acknowledgement work starts, not a frame record. This mirrors `RenderedTranscriptRows.admit` (`widgets/wire_history.py:51–55`), which admits a cropped strip only if it has non-blank text.

Header-only, divider-only, fully covered and blank-body exposures admit nothing. A later layout does not change an admitted frame, because that native admission retains the fragment objects/body runs of its paint and is never rebuilt by reading the widget. PaintedRead/ReadWork remain the separate shared acknowledgement-work state with the phase-2 native start effect.

Completion uses the persistent loop from phase 2. Acknowledgement is scheduled after `paintEvent` returns, through `schedule_ui`, never inline in the paint.

`QPaintEvent` arrival, model insertion and `processEvents` are not admission. Only the paint's own recorded body runs are. The §5.7 Qt controls prove each case against a real `QApplication` when the view is built.

### NRA operations

All available in the installed NRA:
- **New parents, in dependency order:** create the pure key/disclosure/part/notification contracts, shared fragment parents and selected native mixins with `insert_before_target`. Use `patch_target` to attach those parents to the original native classes **before** rebuilding the source index and attempting `promote_class_members_to_ancestor`. The operation is ancestor-only; a parent merely present elsewhere in the file does not satisfy it. Move only the table's shared bodies under its actual binding/MRO/member-authority proof. Move native bodies with traced `insert_class_member`/`patch_target` stages and remove their original copies in the same batch. A dedicated extract-new-base recipe is absent; these existing authoring operations are the bounded route, not a reason to wait or a native-proof claim.
- **`ReadMarkedHistory`:** retain its original relation as WireHistory's ancestor while dropping only its LineHistory base with `patch_target`, reindex, then promote the §5.3 bodies from that actual descendant. The native WireHistory still acquires LineHistory through ConversationHistory. Do not promote an abstract contract stub over the real _mark_read body.
- **Relocation:** `relocate_symbols_to_module` moves the shared declarations to `toad/client/`, and `relocate_module_to_new_module` moves `read_marked_history.py` whole. Every importer is rewritten.

### Finish check

- **No native imports:** `toad/client/fragments.py`, `reader_position.py` and `history_reads.py` import with Textual and Qt blocked.
- **MRO check:** for every member listed above, the resolved owner on `TranscriptFragment`, `BusMessageFragment`, `WireFragment`, `MessageFragment` and `WireHistory` is the same body as on main, either the shared parent or the original native one. Check it by importing and comparing `cls.__mro__` winners before and after.
- **Moved-name consumers:** split logical position imports in `session_presentation.py`, `transcript_preparation.py`, `transcript_publication.py`, `transcript_source_preparation.py`, `transcript_state.py`, `widgets/committed_presentation.py`, `widgets/transcript_history.py` and `wire_history.py` to the shared position owner. Keep their HistoryWindow/PinnedHeader imports native where still required; do not relocate mixed imports wholesale. Update the original geometry, stepped-layout, filter-restyle and presentation-fencing tests; native screen/frame-meter/physical-UI HistoryWindow readers stay native. MessageShown and its native wire-line test import stay native. Part/default/notification moves also rewrite their actual constructor/import sites, not a compatibility re-export.
- **Behaviour:** the §5.7 tests pass, including `test_divider_only_viewport_stays_unread.py` and `test_painted_read_retry.py`, plus all position-family arithmetic/identity and scoped part/role-factory controls above.
