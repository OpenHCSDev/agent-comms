# Bounded source audit: PF1–PF5

Verified against origin/main224 a10655d plus this branch. Read source and actual
callers/diffs; prior handoffs are context, not deletion evidence. This receipt is
bounded to POST-FEATURE-DEBT-AUDIT PF1–PF5, not all original C0/S1-S8 requirements.

- **PF1:** normal `CodingToolSocket` and `SelectedToolSocket` inherit actual
  observation/admission handling from `OwnerToolSocket`/`NativeToolCall`.
  Old announced/started/finished/admitted/denied parallel collections and selected
  _approved/_announced_id/_announced_args/_started/_finished/completed_call_id are
  absent. This batch also removes repeated `resource_claim()` construction;
  CodingTool decodes its typed claim at construction and CodingToolOwner consumes
  it. Separate observation/admission facts and selected one-slot vs coding
  multi-call policies remain real semantics, not compatibility views.
- **PF2:** witness-record/valid-witness/final-seal/pending-seal/write-seal/check-final
  factories and duplicate registry marker key roster remain deleted. Typed
  PrefixSeal/CheckpointSeal, PrefixCertificate and WireMetadata are current owners.
  RegistryStore uses WireLog's decoder; publisher, checkpoint/cursor and route
  callers use typed metadata. Found and removed `_failure` error-constructor
  forwarding. Retained `_saved` performs actual schema/table/row validation and
  decoding; retained PrefixWitness.seal performs the intentional declared projection.
  These are not aliases. Persistence and fsync authority remains on the current bus.
- **PF3:** old always-raising load_native_context_proof remains absent. Repeated raw
  saved-header/user/terminal parsing remains removed. This branch deletes the one
  uncovered `_read_native_context_evidence` wrapper and migrates every source/test
  reference; validation belongs to NativeContextProof.read_evidence. Digest-only
  reader performs its actual digest lookup and still cannot grant context acceptance.
- **PF4:** core resolve_comms_route/ActiveRoute.observe_root still resolves validated
  identity without Comms/Registration construction. Current pinned Toad7a279b0
  comms_root.current_root calls this owner; visible chat refresh checks screen
  activity first; sidebar observations use worker reads. Guarded selected writes
  retain current route revalidation. No retired wire()-for-observation branch found.
  Existing managed default vs explicit root contracts remain supported operations,
  not a duplicate route store. No Toad edits in this batch.
- **PF5:** source-bound HistoryView.capture yields one ChannelDisplayScope or
  DMDisplayScope for each historical source; MessageBus.historical_page uses
  display_page(scope), including scope.index_targets. Old per-row historical
  matches(message,snapshot)/membership rebuilding absent. Any-mode intentionally
  returns unrestricted index targets because sender/peer/mentions can match.
  Remaining _history_page(matches, targets) is shared by real goal/delivery callers
  (goal_actions; goal_management; MessageBus.incoming_page), not an obsolete
  historical callback compatibility path. Source cursors/read bases retained.

No additional parallel/compat implementation was established within these bounded
PF paths after this batch. That statement does not close parent's separately owned
bus/read-ledger/cutover findings, Darwin's channel/catalog scope, or unseen original
plan items. No test/receipt count or module size is used as an ownership proof.
