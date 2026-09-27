# Integrated historical views — implementation handoff

Owner: historical-views Codex worker. No live deployment, owner restart, provider call,
or original-root mutation was performed. Parent owns installation/activation.

## What is implemented

- `MessageBus.attach_history` copies only original bus, registry and presentation/route
  metadata into a destination-owned immutable source. The ordered source manifest is
  owned by the existing MessageBus. Existing BusPageIndex serves bounded source pages.
  No coordinator, delivery cursor, native input, pending attempt or executable authority
  is copied. Original source bytes remain intact; attachment is idempotent.
- Normal Comms channel, DM and combined history APIs include attached sources. Toad
  scrolls into those same sources directly from current channel/DM views. Pages retain
  original sequence, message ID, timestamp, sender and target. A cursor pairs source
  with sequence; source order is display order, never a new message ID/sequence allocator.
  Sources are supplied oldest first, followed by the live bus; this intentionally uses
  source order, not a fabricated cross-bus sequence or timestamp sort.
- HistoricalMessage is a Message display subtype with source provenance and no wake
  policy. Its public original wire representation is unchanged; CLI display includes
  separate provenance. A message older than its source registry's latest declaration
  keeps an unknown incarnation rather than claiming that newer declaration authored it.
- ReadLedger owns separate sparse historical paint evidence. Historical reads never
  advance live human reads or executor delivery. Existing S4 partial channel ACKs
  remain based only on painted live bodies. Toad uses source-bound keys for mounting,
  sorting, pruning and edge cursors, so overlapping sequences cannot suppress rows.
- All preserved registry declarations remain inspectable through `historical_threads`.
  Duplicate creation timestamps and older incarnations are retained verbatim, without
  inserting conflicting executable registrations. Missing historical DM peers can be
  browsed without registering them live.
- Normal Comms screens expose **Saved sessions** (also Ctrl+H). The picker shows source,
  identity, creation time, memberships and original saved-session path. The existing
  TranscriptHistory and Comms transcript parser page that file. Historical sender links
  open the source identity rather than starting/attaching its newer live counterpart.
  No session prompt is submitted and no old UNKNOWN input is replayed.

## Validation

See `core.txt`, `regression.txt`, `real-sources.json`, and the Toad PR evidence.

The preserved-data check copies active presentation/bus metadata into a fixture under
this worktree, then attaches `/home/ts/.agent-comms` and
`/var/tmp/agent-comms-live-20260927-6_d_vdul` READ ONLY. It found 8,400 original public
bus rows and 20 previous-private rows; original public IDs all match their preserved
messages. Channel pagination found 7,595 #comms and 242 #nra rows. #openhcs metadata
exists but neither preserved bus has posts to that channel. All 93 recorded saved
session files across the two source registries opened through the existing transcript
reader; this checks bounded saved tail parsing, not execution/resumption of each agent.

NRA before scan: exact_compact_global, all 79 detectors analyzed, zero omitted,
complete. Existing findings remain; no whole-package clean claim. These are authored
feature extensions of existing owners, not a proved behavior-preserving codemod.

## Parent installation / migration procedure

1. Merge the core and Toad companion PRs after focused local validation. Build/install
   both wheels in the parent's candidate runtime, preserving current native/stack pins.
   This worker did not change dependencies, native execution, ACP/backend consumers,
   coordination schema or recovery. The Toad wheel requires this core API revision.
2. In the candidate runtime run the mounted `tests/historical_views_pilot.py` from the
   Toad branch with the candidate core/UI import paths. Reopen normal Toad after parent
   activation; source reads themselves require no owner restart.
3. Before applying to the active root, back up `history_sources.json` if present plus
   `channels.json`, `channel_metadata.json`, `channel_pins.json`, `saved_views.json`.
   Record absent files as absent. Preserve the original roots. The source copies need
   roughly 8 MB total plus the existing disposable BusPageIndex projections, not caches
   or native bundles. Registry declarations/current owner state are not changed.
4. Preview, then apply using the **new core** Python:

   ```sh
   python scripts/attach_historical_views.py \
     --destination /var/tmp/agent-comms-live-20260927-wzjtqhza \
     --source /home/ts/.agent-comms \
     --source /var/tmp/agent-comms-live-20260927-6_d_vdul
   # Same command with --apply performs the destination-only attachment.
   ```

   Resolve the parent's current active route first; the above destination is the
   assigned route, not permission to substitute a different bus. The operator retries
   attachment safely if catalog restoration was interrupted; sources are attached once.
5. Verify normal #comms/#nra/#any scroll backwards and forwards, historical sender link,
   Saved sessions picker, current DM and current channel send/receipt. Compare live
   bus/native-input state before/after the read checks: browsing must not publish work.
   Existing restored executable identities remain governed by PR136/139. Ambiguous
   identities are visible as historical declarations and sessions, with no authority
   transfer to a current owner. User-directed new execution is a separate explicit action.
6. Roll back display attachment by restoring the manifest and catalog backups (or
   removing only files recorded absent). Restore previous runtime links if reverting
   the UI. Keep `read_ledger.json`: historical receipts are separate and inert when a
   source is detached. Once no UI holds the removed sources, delete only unreferenced
   destination `history/source-*` directories to reclaim their disposable copies.
   Never remove original roots or original sessions. No live bus rollback is needed.

## Actual limits / remaining parent acceptance

- Not installed or enabled on the live route by this worker.
- Snapshot attachment is explicit. Sources are sealed at attachment; later source
  appends are not silently imported. A changed source during copying fails before
  publication. Changes to an attached snapshot invalidate browsing/read proof.
- Source snapshots are grouped in supplied chronology. Equal names across sources are
  shown with provenance; original private/public roots are never merged into one wire.
- Saved-session views are read-only. Current restored sessions keep their existing ACP
  resume path. Historical/ambiguous records do not acquire a new running owner or silently
  seed a new model context. No proof of all 93 sessions executing against a provider is
  claimed or needed for viewing them.
- An empty source boundary can require one final empty page to settle an older/newer
  edge. Page row/byte budgets and oversized-row progress remain unchanged.
- Historical unread receipt facts are stored separately; current unread badges count
  current traffic. Old imported chats do not suddenly wake subscribers or inflate
  their live inbox counts.
