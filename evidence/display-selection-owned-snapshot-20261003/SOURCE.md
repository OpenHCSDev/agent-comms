# Display selection owns a captured read boundary

User report: a transient display-scope error in the open IRC view while ten
messages arrived together. Today's Toad logs and native diagnostics contain no
record of that notification. The exact existing error is raised by
`BusPresentation.snapshot` after three whole-file consistency attempts.

Read first: `DisplaySelection`, `BusPresentation`, `LockedStore`,
`RegistryStore`, `Registration`, `ChannelCatalog`, `ReadLedger`,
`DMDisplayBasis`, `HistoryViews`, and Toad's `ChannelConversation`.
`source-before.json` uses the refactor-audit Package/ParsedModule machinery:
311 Core modules and 289 Toad modules, zero parse omissions. Its 67 selected
declaration/call sites are syntactic evidence, not dynamic-resolution proof.

The existing registry owns both stable identity/membership and changing
heartbeat/turn facts. A file replacement is consequently wider than the
question a display predicate asks (IDEN-7). Three retry attempts and a new
counter would preserve that mistake. `LockedStore.reading` already provides
the resource needed to capture a document while it cannot change.

The owner change uses those existing shared document resources during the
short display capture and opened bus boundary. Raw bus iteration remains
outside those resources. Registry, catalog, and read-ledger values are read
once; `DisplaySelection` derives its fields from those same values. The
optimistic revision loop and its terminal failure are deleted. Channel read
acknowledgement uses the same captured scope, preserving actual identity,
projection and painted-membership checks.

Review the related DM page read for the same whole-registry invalidation
before editing: use its existing identity and scope types, not another
provenance store. Preserve genuine rename, reuse, bus replacement, and
noncontiguous-tail refusal.

## Implemented checkpoint

`6e7ce135` changes two production files: **102 lines deleted, 89 added**, zero
new classes. `DisplaySelection.capture` consumes the documents already acquired
by the original stores. The bus is opened before registry resources, retaining
publication's lock order; raw iteration holds none of those locks. Explicit
Mark Read keeps the original verified history source and marks its exact members,
rather than acquiring another bus high-water while holding the registry.

The complete related DM family now has one existing
`DMDisplayBasis.validate_identity` implementation consumed by fetch and painted
acknowledgement. Its unused read-marker revision field and the whole-registry
file comparison are deleted. Actual viewer/peer incarnation, aliases, bus
replacement and contiguous painted-tail checks remain. Channel acknowledgement
still uses `ChannelDisplayScope.same_projection` and `DisplayBasis.validate`.
`source-after.json` parses 311 Core modules with zero omissions; it records all
29 selected declarations and calls in the two changed modules.

## Verification and limits

- First source batch: 26 passed, three failed. Two activity-clock assertions
  reproduced against the unchanged, installed Core b874 baseline (0.55s); their
  assertions are preserved unchanged in the tests. The third was an obsolete
  mocked opener recursively spawning Mark Read writers; retaining the original
  verified-history path removed that recursion without changing its assertion.
- Final batch: **27 passed, two baseline failures deselected, 23.23s**. The new
  continuous real-filesystem journey uses separate Comms objects while twenty
  channel messages and heartbeats arrive. It pages/acknowledges channel and DM
  views and confirms a real rename still refuses the old DM acknowledgement.
- Actual active-bus source read: five `#openhcs` snapshots returned the original
  thirty-row window, without input, acknowledgement, provider call or launch.
  Each took **2.49–2.73s**. This confirms a remaining performance problem:
  `BusPresentation.channel_page` uses whole-log collection while DM paging uses
  the existing indexed `MessagePageRequest.read` path. The actual bus is 18MB.
  The parent owns the complete shared reader followup; this checkpoint does not
  claim latency improvement or complete IRC/DM reader factoring.
- Actual installed Toad IRC/DM verification is outstanding. The normal 69-package
  holders are currently live, frozen for #390, or retained by the performance
  owner. Borrow a released compatible holder, preserving its original receipts;
  do not create another environment or claim the 15-package Core-only holder
  can run Toad.

Today's source and baseline checks have no native/provider effects. This draft
is a published working checkpoint, not Ready, installed, or a demonstrated
reproduction of the user's transient notification. The notification itself was
not retained in today's Toad/native logs.

## Installed affected App acceptance

The normal69 installed Core5025 candidate passed the original App, saved-bus IRC/DM, exact message-ID and painted-acknowledgement journey. Original20 channel messages plus one DM remain identical on return; human unread counts clear, while the recipient's20 channel and1 DM delivery obligations remain pending. No fake ACP, provider, native input or public effect. Package/source proof covers339 Core,319 ToAd,266 Textual and3 Diff members and native2b trust.

The parent read the complete driver, original App02/05 receipts and cleanup, verified the original05 receipt hash99f8feee874fdad90426d7fc3656e5f925bb4b2942ef7d6585f117ae0e312b5b, and confirmed production/configuration is byte-identical to5025. Earlier driver failures remain archived: renderer entry guard and queries selecting an old hidden view instead of the App's selected workspace owner. The driver now derives selection from that existing owner; production did not change. SVG text was inspected during the run. Black raster exports remain failures of physical readability, not qualified pixels.

Affected installed App/saved-bus acceptance is complete. Exact reproduction of the user's transient notification, whole native-tab/ACP navigation, physical pixel readability and whole workflow performance are not claimed. Default installation is unchanged; the joined receiving release owns ordinary default acceptance. The earlier statement that installed App verification is outstanding is superseded by this result. Original evidence is at evidence/display-selection588-installed-app-20261003; the borrowed holder was released to Einstein after terminal cleanup (five inaccessible own-UID process entries are explicitly recorded).
