# B2 / L0a channel creation and routing retirement

Branch: `refactor/channel-deletion-closure-20260928`, core base main #224 (a10655d).
Paired Toad: same branch name in OpenHCSDev/toad, base main #73 (7a279b0).

## Implemented and deleted

- `channels.py`: exact tag channels only; removed `Channel.aggregate_target` and `members_for`. A saved declaration owns its predicate and retained original targets. The existing Channel presentation snapshot carries that declaration for normal displays; it does not confer routing authority.
- `catalog_document.py`: deleted runtime `audiences`, `set_channel`, `delete_channel`, union audience matching/rename/restore branches. Saved views appear through existing sidebar/history/read owners, retain preferences, pinned members, parent/archive and creation date. Restoring a same-named view retains original-target history without replacing the current predicate. Imported targets cannot be silently freed for reuse: archive their view instead of deleting it.
- `catalog_store.py`: deleted ALL converters and four-file readers, custom decode and `source_paths`. Runtime reads precisely current `CatalogDocument` through FieldCodec. No old-format fallback.
- `channel_management.py`: deleted old creation/deletion entry points; membership notifications use `publisher.publish_ordinary` and never publish to a saved view.
- `tools.py`: removed only `comms_set_channel`/`comms_delete_channel` declarations and handlers. Current tag/view tools remain.
- `publisher.py`: removed explicit audience construction and revision mirror; initial publication resolves the canonical catalog after the existing saved-view rejection.
- `history_views.py`, `read_basis.py`: capture SavedView's original/exact targets for history and painted read evidence. No original row, target, ID, sequence or delivery authority is rewritten.
- `message_bus.py`: narrow catalog consumers only: one catalog revision path and no retired metadata copies during history attachment.
- Toad `comms_chat.py`: read-only composer/status and rejection of direct submission for saved views. Current callers/tests use exact tags or saved views; all old `set_channel` calls removed. Normal exact-channel send/tab/draft behavior remains covered.

## Current evidence

- `current-format-core.log`: **78 passed**, includes channel/view lifecycle, pins/sorts, history pagination/read display, roster restore, private human initial admission, membership and tool consumers.
- `guard.log`: **1 passed**; AST guard prohibits retired channel methods/audience state, old tools and catalog decoder/classes.
- Paired Toad `current-format-mounted.log`: **exit 0**, normal sidebar keyboard navigation, original-target + exact-channel transcript, pin retention, disabled composer/direct submit refusal, byte-identical wire.
- Paired Toad `exact-channel.log`: **exit 0**, normal exact-channel send, member changes, participants, tabs/drafts and pins. (Receipt's older print label says union; fixture actually creates an exact engineering tag.)
- `saved-catalogs-cutover.json`: real metadata from three roots read only, copied, converted and reopened. All preferences/current projections retained (16 + 6 + 27 preference records). Exact-channel declarations collapse to tag channels. No real union definitions were found. Synthetic saved union conversion additionally preserves predicate/preferences/original target and unchanged wire bytes.
- Earlier 82/59 receipts predate the owner's no-runtime-converter correction and are not the final acceptance. Retired-reader tests were deleted; retained behavior tests use current declarations.
- No provider calls, live writes/restarts/install, CI wait or native package changes. Full merged-tree suite is parent's integration responsibility.

## Quiet cutover / parent integration REQUIRED

This source is publishable; L0a is **not install-complete** until parent executes and deletes the temporary tool.

1. Integrate both source PRs. Preserve B1's publication/guard changes. The only shared B1 hunks are catalog-path consumers in MessageBus/Publisher/HistoryViews; `ChannelManagement.update_tags` now calls `publish_ordinary`, so B1 can delete `Publisher.publish`.
2. Stop owners with no turns/compactions in flight. Retain the normal recoverable install backup. Classify `catalog.json` and the four retired catalog files as durable user definitions/preferences; wire/history are durable and unchanged. Catalog projections/read cursors are derived and may be reset by the parent cutover.
3. With candidate source on PYTHONPATH, run `python tools/cutover/channel_catalog.py ROOT...` for a read-only preview. Roots must include the selected bus, preserved sources that will be attached, and already attached `history/source-*` snapshots. Runtime no longer imports four-file metadata, so do this BEFORE new history attachment/install. Do not reset catalog preferences or wire rows.
4. Run the same command with `--apply` at the quiet install. It takes wire then document locks, atomically publishes current catalog, deletes the four superseded metadata files, and never writes wire/history. It refuses an ambiguous tag/view-name collision without dropping either definition. No such collision was observed in the three actual roots. Reconcile such a source explicitly if encountered.
5. Verify installed normal channel/view path and restart normal owners. Then delete `tools/cutover/channel_catalog.py` and its one-shot evidence runner `saved_catalog_check.py`; retain receipts. This deletion is a required part of parent's install completion, not deferred polish.

Existing shared `guard_legacy_root_write` import/call in `channel_management.py` belongs to B1's global active-route retirement/rename. No alias is added here; B1 must close that shared identifier for the full L0 zero-marker guard. The channel/catalog deletion guard itself passes. CatalogStore/Document/Channel contain no forbidden compatibility markers.

## Store boundary / limits

Saved view creation cannot invent original-target provenance. Migration is confined to the temporary operator tool. Imported view editing preserves provenance; tag rename preserves original addressed target history. Sources are not rewritten or replayed, and old inputs gain no execution authority. Runtime cannot read a pre-cutover catalog: installation order matters. No automatic history/catalog conversion remains in src.

## Line accounting

- `src/`: 91 added, 275 deleted.
- `tests/`: 232 added, 275 deleted.
- `tools/cutover/`: 182 added, 0 deleted.

Temporary cutover additions preserve actual saved user definitions outside runtime; parent deletes the tool after install. Production and test surfaces both have net deletion.
