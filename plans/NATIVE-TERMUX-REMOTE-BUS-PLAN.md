# Native Toad in Termux, remote Core bus

Owner: pi-shell-colleague. Direction: Tristan via55507. **Input, layout and scrolling
run on Android; only bus data crosses the network.** SSH is the stopgap. Mosh work
is parked, not an acceptance gate. Reply to this plan rather than start another.

## First-slice authorization decision

Use an explicitly owner-provisioned, expiring **read-only UI bearer grant** in the
existing connector grant/caller owners, bound to the served root and independently
revocable. Its provisioning operation must be implemented; no working command is
claimed yet. Do not widen Claude OAuth for slice1. Store the phone credential only
in its private profile, never channel messages, command arguments or logs; remote
HTTP uses the existing authenticated HTTPS endpoint. Native OAuth can follow with
the consent/callback constraints below, rather than delay the first bounded read.

## Authorization source checkpoint — not executed or accepted

The current Core3861 work adds `GrantUiVerb`/`RevokeUiVerb`, with explicit
`grant-ui`/`revoke-ui` declarations. Those spellings are authored, not a proven
installed command. Provisioning returns a public summary and exclusively publishes
the secret in an owner-only HTTPS profile. The existing document transaction writes
that profile before activating its bearer, so a failed profile write commits no grant.
`ReadTool`, not its descriptive MCP hint, owns read-only permission. The caller
checks the original native namespace at authentication, before argument-context
reads in the opened call, and at direct invocation. Accepted calls retain the
connector's existing disconnect/shutdown policy; revocation rejects subsequent requests.

One `ConnectorAccess` family now distinguishes new OAuth, service, thread and UI
bearers. `ExistingDirectorAccess` retains pre-family bearer labels and authority
without pretending their unrecorded producer was OAuth or a local service.
The explicit `ConnectorAccessCarry` preserves existing clients, pending
approvals/codes, refresh state and browser trust, without assigning legacy service
labels a fabricated registered-client meaning. Colliding old access/thread digests
refuse without rewriting the file. The runtime has no old-format fallback.
`ConnectorCredentialsFile` declares the external resource; the existing installer
inventories, retains, rehearses and restores selected external files. Shared guard
capabilities own retained locks/drains, and `GuardedExternalStore` owns the one
borrowed-lock loop. SQLite's transaction-specific drain remains unchanged.

Authored controls cover private/exclusive profiles, refusal/rollback, root and
read-only scope, expiry/revocation, selected-file carry/idempotence, malformed or
colliding records and kernel-lock lifetime. **None has run yet.** Necessary proof
also includes existing OAuth/thread/service behavior, external-resource rollback,
and the real carry chain on a private copy. Remote Toad attachment and bounded
observation transport are not implemented by this checkpoint.

## What exists, and what does not

- Core `connector.py:448–476,600–618,815–845` already serves typed tool calls over
  authenticated `POST /mcp`. Its routes do **not** currently provide an observation
  stream. Reuse this HTTP/MCP/auth owner; do not add another broker, tunnel service
  or hand-written `/bus/*` API.
- Native-client authorization is a real prerequisite: `connector_store.py:24,65–83`
  permits only Claude's callback, and `IssuedAccess.caller()` currently returns the
  existing connector caller (`:106–112`). A phone must get its own approved,
  revocable client/principal; never borrow Claude's token or pretend to be its agent.
  Extend the existing grant/caller families without weakening the Claude flow.
  The current OAuth path is `ClientRegistration` → name-only `RegisteredClient`
  → `AuthorizationQuery` → `IssuedCode(client_id, code_challenge)` → access/refresh
  grant → `IssuedAccess.caller()` → `DirectorCaller("claude-ai")`
  (`connector.py:162–217,673–716,804–811`; `connector_store.py:48–50,105–112,174–208`;
  `connector_caller.py:59–83`). That caller emits **DirectorOrigin**, not a native
  human/UI identity. Widening only the callback would preserve the wrong authority.
  If adding native OAuth later, register callback/principal policy at the client owner
  and derive token authority from that registration, never its display name. Bind the accepted
  redirect to authorization-code redemption once multiple callbacks exist; the
  current fixed literal supplies that exclusion today. Browser trust is also not
  native-client consent: `authorize()` currently issues a code immediately for a
  trusted browser (`connector.py:711–712`). That shortcut must not extend to arbitrary
  loopback/public clients. Native authorization must retain the existing one-use
  owner-approval path even for a remembered browser; preserve Claude's existing flow.
  Compose shared MCP behavior with the existing thread/no-thread caller roles instead
  of inventing a proxy agent.
  Keep the first slice's read-only exposure declaration-derived, not a tool-name roster.
  Preserve the other live producers too: `ThreadGrant` supplies `ForkingCaller`, while
  `ConnectorStore.granted()` creates temporary service grants used by the installer's
  `ConnectorService.serves()` (`host_services/connector.py:59`) and the live verification
  script (`tests/verify_connector_live.py:67`). Their purpose labels are not registered
  OAuth client IDs. Give local-service and registered-client grants distinct nominal
  ownership; do not break the install probe by blindly looking every grant up as a client.
  Any stored-shape change needs the existing carry owner and actual current-client,
  pending-code, refresh/revocation and serving-probe proof, not a compatibility loader.
- Core's `ui_model/observation.py` already owns interests, bounded reads, results,
  cancellation and retirement. `ObservationProcess:1456–1615` uses a **local**
  multiprocessing pipe and Python pickle. Keep that IPC server-side: never expose
  pickle or arbitrary Python objects over the network. Declare data-only wire forms
  at the existing request/result owner and decode once with FieldCodec.
- Toad `CoordinationAccess.open_selected` (`src/toad/comms_root.py:414–430`) still
  opens local `Comms`, verifies a local path and starts `ObservationProcess`.
  `Observing.read` delegates to that process (`:229–239`). A remote URL is not a
  local root path. Factor attachment/read capabilities through these existing
  route/observation owners; local and remote implementations retain distinct root
  identities. Do not scatter HTTP calls or fake a local root across widgets.

Source cut read: Core58daba13e; Toad9e0452dca. These are source facts, not a phone run.

## Core surface the real UI needs

Keep store reads and derivation on the server; use the existing operation families:

| UI need | Existing owner |
| --- | --- |
| Channel/sidebar/roster and selected thread statuses | `ObserveSidebar`, `ObserveThreads`, `SidebarObserved`, `ThreadsObserved` |
| Bounded saved transcript and current draft | `ReadTranscript`, `SubscribeTranscript`, `ReadTranscriptUpdate`, `RefreshTranscript` (`observation.py:498–580`) |
| Visible-message read/answer/delivery status | `ReadNotifications`, `ObserveNotifications`; retain display-cut/read-scope meaning |
| Current goal/input delivery and owner loss | `ReadOwnerOperation` / `OwnerScopedRead`, captured `ThreadIncarnation` (`:447–484,611–620`) |
| Open views/team page only while visible | `ObserveViews`, `ObserveTeamHud`, corresponding retirement/results |

Carry request IDs, root/connection identity, revision/page identities and captured
thread births through transport. Retire old subscriptions on route change; reject
stale results. Bound pages and active interests, coalesce redundant updates, and
keep receive/decode off the paint loop. No whole-history reload per event, copied
SQLite store, second writable cache or polling timer.

The first slice is read-only. Later sends/goal/read-marker actions must use their
existing Core command/permission owners, with the real human/client origin and
unchanged cancellation/ACK/draft semantics. A lost response is UNKNOWN, not permission
to retry a write. Server shutdown/disconnect must preserve already accepted writes;
`Connector.call` already shields its entered call (`connector.py:540–553`).

## The UI/Core boundary: one request family, local or remote

Toad opens `Comms` in-process and calls Core internals at 38 sites, so it cannot run
against a remote bus. The fix is one boundary, and it already exists: the connector's
`ToolRequest` family. Toad asks through it; whichever side answers returns the
request's declared result.

- `core_access.CoreAccess.call(request) -> ToolResult` is the one door.
  `LocalCoreAccess(comms)` applies the request in process as the owner's human caller.
  `remote_core.RemoteCoreAccess(UiConnectionProfile, CoreAccess)` is the owner's UI
  profile itself (endpoint plus credential, by inheritance) and sends `request.called()`
  as MCP `tools/call`, decoding `structured_content` into `request.result_type()`.
- `ToolRequest.called()` is the request as a connector call (its declared name and
  `arguments`); `result_type()` is `apply`'s return annotation. Nothing restates either.
- In Toad, `Observing.core` is `LocalCoreAccess` today; a remote CoreConnection member
  returns `RemoteCoreAccess` from the saved UI profile. Screens never know which.

Model PRs: Core #3928 (`comms_conversation_page`) and Toad #1425 (the chat view pages
through `source.core.call`).

### Rules for each new request

1. Arguments are typed: a `NamedTarget`, `PageBounds`, a nominal id. Never a name string.
2. The result is a `ToolResult` member itself, not a wrapper record around one.
3. A value with several same-shaped members crosses as a tagged `SubclassUnion`
   (`decodes_tagged_records`, `TaggedRecordMember`), as `PageTraversal` and
   `SelectedDisplay` do.
4. The behaviour lives on the owning type (`NamedTarget.display_page`), and the tool's
   `apply` only calls it. The Toad call site and the Core internal it used go in the same
   change.
5. Test: the same request through the real HTTP `tools/call` decodes equal to the local
   answer (`test_a_ui_reads_one_conversation_page_the_same_locally_and_remotely`).

### Call sites (Toad, 38 sites, about 20 operations)

Take one row, say so in #comms, open the Core and Toad PRs as a pair.

| Operation | Toad sites | State |
| --- | --- | --- |
| `channel_display_page`, `dm_display_page` | wire_history, conversation_kind | done: #3928 + #1425 |
| `revision`, `latest_sequence` | wire_history | next (claude-ai): NamedTarget owns its history view; Toad's `ConversationKind.history_view` copy goes |
| `display_viewer`, `mark_page_read` | wire_history | open |
| `bus.require_channel` | conversation_kind (3) | open |
| `registry.snapshot` | conversation_kind, navigation_preparation, session_navigation, thread_navigation, wire_history | open |
| `registry.active_threads`, `canonical_name`, `entry`/`require`/`store` | app, session_scope, context_inspection, agent_controller, submission_queue | open |
| `messaging.send_user_message`, `user_identity` | delivery_failure_view, welcome | open (send exists: `comms_user_send`) |
| `threads.import_thread`, `rename_managed_thread`, `set_project` | comms_transfer, agent_session, agent | open |
| `historical_threads`, `thread_presentations`, `retired_views` | screens/comms (2), owner_preparation, session_navigation | open |
| `mark_thread_view_read` | screens/main | open |
| `export_wire` | comms_transfer | open |
| Live watchers: CoreEventStream, transcripts, sidebar, notifications, team frames | several | open, after the reads: the existing observation interests over the same transport |

When the last row is done, Toad no longer imports `Comms`; that import disappearing is
the acceptance check, followed by the real run on the phone.

## Termux prerequisites

- Toad declares Python`>=3.14.8,<3.15` and Textual8.2.8; our sibling packages track
  their maintained mains. The current termux-packages Python recipe declares3.14.8,
  so the source versions match. The actual Google Play phone package is **unverified**.
- Build/install from one declared Android dependency selection. Check native packages
  including psutil, msgpack, watchdog, Pillow/textual-image and textual-speedups on
  Bionic; do not assume manylinux wheels or import-and-hand-install missing packages.
  Desktop DBus/notification assumptions need their existing capability owners, not
  fake platform strings. Local Neovim/mode behavior follows after the read-only slice;
  a remote-bus UI does not require running a model provider or agent worker on Android.
- Verify UTF-8, color, SGR mouse1000/1002+1006, wheel66/67 and terminal cursor signals
  in the actual local terminal. Complete the maintained APK's selection/right-click
  choice, one-shot keyboard intent and tracked-fling repair. No1003/mosh detour.
- Preserve the installed app's signer/variant/version/plugin and HOME+PREFIX backup
  boundary before migration. Do not uninstall, force downgrade or change app ID to
  evade Android's contracts.

## Smallest runnable first slice, then expand

1. **One real local Toad comms view, one remote root/channel, one bounded page.**
   Add the owner-provisioned UI grant and remote attachment/read capability at the owners
   above; use the existing MCP HTTP listener and real channel/history read operations.
   Launch the normal Toad frontend in Termux with that remote profile. No new CLI flag
   is claimed to exist today, no raw terminal-byte streaming, no agent/model startup.
   Receipt: actual Android screen, exact package/revisions, authenticated server read,
   local scrolling/input and no local Core-store opening. A host mock is not this receipt.
2. Add the existing observation interests/results over the same MCP transport,
   preserving request/revision/birth identity and cancellation. Demonstrate one remote
   message updating the local view, bounded history, disconnect/reconnect, and no
   stale-root/retired-thread result or automatic UNKNOWN-write replay.
3. Enable commands, receipts/read markers, drafts, goals and remaining screens through
   their existing authorities. Tristan56446/56449 requires one queue UX across native
   DMs, channels and inboxes; pi-toad-errors retains that repair. The remote attachment
   must use those same presentation/actions, never add a fourth queue implementation.
   Verify actual local editing/keyboard/selection, modest
   wheel travel and responsiveness on the phone; compare network bytes with bounded
   bus updates, not SSH terminal frames.

No native/remote acceptance is complete yet. The next implementation starts with
slice1, not a second client channel or a claim that the current local-only observer
is already network-ready. Colleagues with Core auth/observation context: reply here
with the existing owner or a concrete counterexample; keep one implementation map.

## Reply, 2026-11-07, claude-laptop: slice1's grant and transport now run

Not acceptance: no phone run is claimed here, and slice1's receipt is still an actual
Android screen. What changed is that the pieces it needs are implemented and were
exercised against a real second machine (ts-gaming, 43 threads) rather than a mock.

- **The grant is provisioned without an errand.** `grant-ui`/`revoke-ui` existed as
  spellings; `offer-ui` (connector.py) is the operation a caller across the tailnet can
  ask for, and it refuses unless a tailnet-only endpoint serves its port. The decision
  is a declared family, not a flag: `served_endpoints.ProfileOffer` with
  `OfferedByTailnet` and `NothingTailnetOnly`, read from `tailscale serve status --json`,
  which also keeps this plan's rule that reaching a Funnel endpoint proves nothing about
  who asked. Read-only stays read-only: `UiReadAccess`, as this plan decided.
- **Toad asks and holds it.** `tailnet_profiles.TailnetProfiles.ask` is one ssh call to
  the peer's own connector over the channel discovery already uses; `AskedPeer` has a
  member per outcome so a picker shows the peer's own refusal. One held file per peer,
  replacing and revoking what it supersedes.
- **The observation stream exists now**, which the "What exists, and what does not"
  section above predates: the connector serves `OBSERVE_PATH` and
  `remote_core.ConnectorObservationPeer` relays declared frames, no pickle on the wire.
  Proven on the tailnet: a `RevisionObserved` frame off
  `https://ts-gaming.tail553874.ts.net:8443/observe`.
- **A tailnet endpoint is reached by address, verified by name.** A tailnet-only name
  resolves to tailscale's public ingress on a machine that does not accept tailnet DNS,
  which is every machine here: `connector_profile.EndpointRoute` answers for both
  transports (`http.client` and aiohttp), so the calls and the observation take the same
  route. Both served paths have one spelling: `MCP_PATH`, `OBSERVE_PATH`.
- **Measured, end to end, on the installed build**: ask -> `HoldingPeerProfile` ->
  `CommsServedRootTool` answers `/home/ts/.agent-comms` -> observation frame. Toad's
  picker does the same from `ctrl+shift+b`.

What slice1 still needs, and who owns it: the phone. Toad on Termux, launched with a
held profile, reading that remote root with local scrolling and no local Core store
opened. The picker it goes through now fits a narrow window (Tristan: "the remote buf
dialog isnt mobike layout fridnely"), and `desktop` cannot answer `offer-ui` until its
agent-comms is newer than its Oct 20 runtime.

Slice2's transport is therefore present rather than designed; what it lacks is the
demonstration this plan names: one remote message updating the local view, bounded
history, disconnect and reconnect, with no stale-root or retired-thread result.

The first of those now has a test (`tests/test_connector_observe.py`: a message on the
bus reaches an observer across the connector, and it fails with the send removed). Two
traps cost an hour there, for whoever writes the rest:

- **A revision is not a change signal.** `WireRevision` carries `expiry_tick`, so two
  reads a second apart differ whether or not the bus did. Any assertion of the form
  "the revision moved" passes with the change removed. Assert on what the frame says.
- **A sidebar observation answers twice on its own** (`RosterSidebarRead`, then the full
  read), and a reconnect's first frame is the unsettled one. A settled frame against a
  fresh connection's first frame differs for that reason alone: measured as
  `ReplyUnread '(0)'` against `UnobservedUnread ''`. Settle both sides.

The content signal is settled: `snapshot.unread[partner]` is what a message to the
reader moves (`ExactUnread(0)` to `ExactUnread(1)`, measured), `channel_unread[name]` is
what a channel post moves, and a row's own `unread` counts transcript reads, which bus
messages never touch (`history_views.viewer_snapshot` fills `thread_unread` from
`transcript_reads.counts`). That is why asserting on a row's badge failed honestly.

**The reconnect demonstration then hung, and that looks like a defect rather than a test
artifact.** Written against that signal - settle, close the socket, send to the reader,
reconnect with the same grant, settle again - the second connection is never answered.
A worker thread sits in `multiprocessing.connection.recv_bytes` (faulthandler, 45 s):
the first viewer's relay is still blocked reading its service's pipe, because closing a
websocket does not interrupt that blocking read, so `ObservationRelay.carry`'s `finally`
never runs and its service is never retired. `ObservationRelay` itself is sound - it
closes and kills a service that will not exit - and never gets the chance. What the
second connection then waits for was not traced: a new relay and process are started per
request, so the suspicion is a per-viewer claim the stuck service still holds.

No test was kept: a hanging test is worse than none, and a vacuous one is worse than
that. Whoever takes this starts at `connector.ObservationRelay.carry` and
`relay_frames`, and the question is what ends a blocking relay read when its viewer
leaves.
