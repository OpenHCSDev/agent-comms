# R2 — catalog document ownership and declared message boundary

Owner: Darwin. Source `5d867f4`, reconciled with main190 (`8300308`) in `cf91479`; R4's actual OwnedTurn lifecycle/error consumer changes are retained. Branch `refactor/catalog-document-ownership-20260928`, persistent tree `/home/ts/wt/comms-catalog-document-ownership-20260928`. Parent owns paired Toad, serial integration and activation. No live root writes, provider calls, extra workers or models.

## Complete replacement / actual owners

- **CatalogDocument** (`catalog_document.py`) owns persistent tag declarations, named audiences, channel preferences, saved views and list ordering. **ChannelPreferences** owns each channel's order/date/pin/parent/archive/any-mode/member pins. Actual lookup, rename/delete, validation, tag/view changes and restoration operate on this document; no mutable catalog cache mirrors it.
- **ChannelCatalog** (`catalog_store.py`) is the existing A8 `LockedStore[CatalogDocument]`. Canonical filename is declared there: `catalog.json`. Shared reads, exclusive edits and durable atomic replacement use A8. All preferences and audiences commit in the same document; an any-mode change cannot partially publish one of four files. A read returns an independent document, not a facade over shared mutable state.
- **CatalogMigration** declares the four actual retained source formats and filenames: channels.json, channel_pins.json, channel_metadata.json, saved_views.json. FieldCodec decodes them; saved-view map identity and missing historical timestamp0 are normalized once. Until the first write, readers project those inputs without mutating them. First successful mutation atomically publishes the complete canonical document. Thereafter only catalog.json participates in reads/revisions; old files are retained historical input bytes and never merged from or written again. This is saved-data migration, not old-writer coexistence.
- **MessageWireCodec/Message** now derive decoding from dataclass fields through FieldCodec. Only actual external boundary details remain: derived ID/private receipt separation, missing historical timestamp0, preserving original numeric timestamp spelling (including int and nonfinite historical values), and the existing independently validated ClaimTransition parser. Claim/private proof checking is not replaced. Adding a declared Message field works without another decoder list. Constructor role/membership coercions and ThreadMention.from_wire's duplicate parser are deleted; current callers use typed values/codec decoding.
- The empty Message.to_display_wire forwarding method is deleted. CLI's three normal history outputs compose canonical to_wire with declaration-owned display_metadata; live metadata is empty and HistoricalMessage owns provenance metadata. Historical public encoding and original IDs are unchanged. No message registry or second serializer is added.

## Caller / deletion map

| Source | Current owner/contract and removed mechanism |
|---|---|
| `channels.py` | Keeps Channel/SavedView/ViewPredicate declarations. Entire605-line stateful ChannelCatalog removed: private field caches, four-file revision cache, read-unpack/write API, sidecar paths/writers and metadata compare/write mechanism. ChannelCatalog import moves to catalog_store; no re-export. |
| `channel_management.py` | Wire lock → registry snapshot → catalog.editing(); document owns mutation/validation. No direct catalog raw read/write or metadata cache manipulation. Tag rename/delete still uses the existing cross-registry wire transaction; no invented multi-file atomicity claim. |
| `thread_management.py`, `nk_foreground.py` | Existing wire-owned registration/rename/delete transactions edit the same document for dates and pins. No registry acquisition under the catalog lock. |
| `history_views.py` | Reads CatalogDocument directly; display basis captures one document for channel/preferences/order. Restore imports the preserved staged source and edits only destination. Observer/display revision paths use canonical catalog or its migration input paths. |
| `message_bus.py` | Delivery/history audience resolution reads the document. Pending revisions include the determining catalog source paths. History attachment copies canonical document plus declared historical input files, preserving original snapshots. No live source files changed. |
| `publisher.py` | Private admission captures registry plus catalog source paths before decoding, binds document audience and its revision, and rechecks the captured files before append. Canonical absence is included before migration, so creation during capture invalidates it. No independent catalog/audience authority. |
| `exporting.py`, `tools.py` | Current catalog reads use typed document; existing export/tool schemas unchanged. No old-write projection retained. |
| `input_drain.py`, `owned_turn.py`, `owner_compaction_commit.py` | Only catalog target reads/imports migrated. Queue, compaction, cancellation and R4 lifecycle policy untouched. |
| `messages.py`, `mentions.py` | FieldCodec boundary replaces Message's hand-listed decoder and mention parser/coercions. Current canonical declared message fields only; unknown extension input is rejected rather than silently ignored. No actual saved row is rewritten by this change. |
| `historical_views.py`, `cli_commands.py` | Actual historical metadata hook and all three CLI consumers migrated; old to_display_wire API absent. |
| Tests | Existing channel/history/roster/passive/view consumers use current document contract. Saved four-file fixtures remain meaningful migration coverage. Removed the test requiring older live writers to overwrite a stale document while pins survive, and the speculative future_extension acceptance line from the otherwise retained lossless export test. No old-interface fixture adapter added. |

`callers.txt` records the source search. Remaining hits for `catalog.resolve/views/...` are actual CatalogDocument locals, not obsolete store methods; test_native_pi's models.json variable is unrelated.

## Paired Toad contract for parent

No Toad tree edited. Current parent tree search identified production `src/toad/navigation_preparation.py:52` and `src/toad/widgets/comms_sidebar.py:1396`: `comms.channels.catalog.resolve(x)` → `comms.channels.catalog.read().resolve(x)` (preserve each actual Comms receiver). Tests: channel_views_pilot.py, session_sort_pilot.py, channel_any_mode_ui_pilot.py have the same resolve migration; list_order becomes `.read().list_order`.

Full contract if other current callers arise:
- import ChannelCatalog from `agent_comms.catalog_store`, not channels;
- `catalog.read()` → CatalogDocument;
- document fields: `tags` (declared only), `audiences`, `preferences`, `saved_views`, `list_order`;
- `document.all_tags(registry_threads)` and `document.views(registry_threads)` include current registry membership;
- `document.resolve`, `targets_for`, `history_targets`, `is_view_target`, `pinned_threads(channel)`, `pinned_members()`;
- determining persistence revision: `catalog.revision()` or `catalog.source_paths()` for a captured multi-source check;
- normal UI writes remain through Comms ChannelManagement's existing mutation methods.
No current Toad use of ThreadMention.from_wire or to_display_wire found in that source/tests search.

## Migration and lock procedure for parent

Deploy paired callers before resuming old executors; there is intentionally no simultaneous older-writer support. Install alone does not rewrite catalogs: first real catalog mutation migrates automatically. For explicit migration without changing preferences, use the installed Comms object's existing wire lock then `catalog.editing()` with no document edits; absence of the canonical file causes the full lossless atomic write. Do not nest another store.read inside editing. Lock order remains **wire → registry snapshot (released) → catalog**, and no catalog method obtains registry/wire locks in reverse order. Read-only views use A8 shared document reads.

The old four files remain unchanged recovery/source bytes. After successful publication the new runtime ignores them permanently. They are not a downgrade target after new edits: older software cannot consume subsequent catalog.json updates. A rollback after new edits must preserve the canonical document and migrate its values, not simply delete it to resurrect stale inputs. No live rollback/migration was attempted here. A failed initial replace/directory sync leaves original sources and no partial canonical document; failed subsequent writes restore the previous canonical inode through A8. Unknown/malformed source data fails without dropping fields or writing a partial destination.

## Focused evidence

Interpreter `/home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python`, absolute owned `PYTHONPATH=.../src`, `-m pytest -o addopts=''` disables xdist/coverage defaults. All batches use60s shell bounds. Persistent fixture root `/home/ts/wt/.cat28` after the first channel batch. No CI wait.

- `second.txt`: **97 passed**7.80s — test_channels, test_channel_display, test_saved_view_boundary, test_roster_restoration, test_mentions, test_historical_views. Normal catalog/channel/DM history, any-mode/read semantics, provenance, date/pin/parent/sort migration and restore/current consumer behavior.
- `boundary.txt`: **135 passed**10.94s — test_catalog_document, envelope bus/claim transitions, private human ingress, exports, viewer/index/pages and passive-channel awareness. Preserved catalog data, concurrent writers, migration fsync rollback, observer revisions, actual private audience seals and public formats. Overlaps later focused cases; counts are not summed.
- `consumers.txt`: **88 passed,1 failed**6.00s — normal Message/MessageBus/CLI/user channels/S4 and real SQLite selected-runtime mocked-model cases passed. The new revision-race fixture patched freeze_audience on Publisher instead of its declaring audience_manifest module; fixture corrected without changing production behavior.
- `revision.txt`: **40 passed**1.43s — all9 new catalog/message cases plus MessageDeclaration, claim transitions and private human ingress. The exact revision-race fixture now proves a catalog mutation during freeze prevents any append. Integer timestamp identity preserved; new dataclass field codec derivation; malformed source failure and atomic retry.
- `merged.txt`: **66 passed**5.33s after main190 merge — catalog boundary, InputDrain, channels/restoration and actual selected-runtime SQLite with local mocked model (engage/ignore and passive no-model). R4 consumer changes retained. No native/provider acceptance or Toad mounted-pilot claim from this worker.
- `nra.json`: exact_compact_global,79 analyzed/0 omitted,complete,0 findings, whole src context and report targets catalog document/store, ChannelManagement and Message. Manual semantic edits, not a synthesized equivalence proof. Scan ran before the final filename/caller polish; no architectural clean-pass inference beyond that coverage.
- Changed Python files parse; Black applied; git diff --check passes. No Ruff claim (not installed in the selected environments).

Earlier red attempts are retained: first.txt identified incomplete consumer/old-test migrations; boundary-first.txt had three fixtures using the wrong persisted ThreadSort spelling and the now-removed speculative future field expectation. Their relevant corrected batches above pass. No truncated/timed-out suite is called green.

## Remaining boundary

Core R2 implementation/caller deletion is complete and published for parent integration. Parent owns paired Toad and installed migration/acceptance. No live data copied into this evidence and no historical inputs replayed. Owned disposable audit/test caches are cleaned after all test/scan processes exit; retain source fixtures and these receipts.
