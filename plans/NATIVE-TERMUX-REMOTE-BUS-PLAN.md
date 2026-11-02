# Native Toad in Termux, remote Core bus

Owner: pi-shell-colleague. Direction: Tristan via55507. **Input, layout and scrolling
run on Android; only bus data crosses the network.** SSH is the stopgap. Mosh work
is parked, not an acceptance gate. Reply to this plan rather than start another.

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
  The complete current chain is `ClientRegistration` → name-only `RegisteredClient`
  → `AuthorizationQuery` → `IssuedCode(client_id, code_challenge)` → access/refresh
  grant → `IssuedAccess.caller()` → `DirectorCaller("claude-ai")`
  (`connector.py:162–217,673–716,804–811`; `connector_store.py:48–50,105–112,174–208`;
  `connector_caller.py:59–83`). That caller emits **DirectorOrigin**, not a native
  human/UI identity. Widening only the callback would preserve the wrong authority.
  Register native callback/principal policy at the client owner and derive token
  authority from that registration, never its display name. Bind the accepted
  redirect to authorization-code redemption once multiple callbacks exist; the
  current fixed literal supplies that exclusion today. Compose shared MCP behavior
  with the existing thread/no-thread caller roles instead of inventing a proxy agent.
  Keep the first slice's read-only exposure declaration-derived, not a tool-name roster.
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
   Add the native-client grant and remote attachment/read capability at the owners
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
   their existing authorities. Verify actual local editing/keyboard/selection, modest
   wheel travel and responsiveness on the phone; compare network bytes with bounded
   bus updates, not SSH terminal frames.

No native/remote acceptance is complete yet. The next implementation starts with
slice1, not a second client channel or a claim that the current local-only observer
is already network-ready. Colleagues with Core auth/observation context: reply here
with the existing owner or a concrete counterexample; keep one implementation map.
