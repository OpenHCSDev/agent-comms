# R3 parent integration checkpoint — source acceptance in progress

Parent owns paired callers and installed activation. Core implementation is Darwin's refactor/input-disposition-documents-20260928 branch, reconciled to main197. R1 remains live and usable. R3 is not claimed merged or installed.

## Completed parent evidence

check_saved_inputs.py captured only input_dispositions.json/acp_delivery_cursors.json under their shared locks into an owned ~/wt fixture. Old installed versus new typed documents agree across3 roots: original7766 +live14 input rows,96 original delivery cursors,2580 total UNKNOWN records. Typed publication/readback ran on owned copies only; no live input sends or mutations. All complete records and cursor values compared after normalizing omitted notice/review defaults; no other differences accepted. Compact receipt saved-inputs-first.json. Removed33.9MB copied files after acceptance; original documents untouched.

Paired Toad97 is a draft with four migrated direct core-facing test consumers. Source current_delivery_owner, input_delivery_owner (932 legacy notices), queue_view_backend and native_input_attribution (both native/saved) pilots pass. Production public ACP payloads are unchanged; tests use actual current owners instead of obsolete forwarding APIs. Parent identified the shared codec/native wire_tag naming collision during review; worker now declares family_discriminator independently of the native external selector. No compatibility aliases restored.

Parent combined current R1/R3 FieldCodec, Pi payload, input document and nominal RPC boundary batch passed96cases in1.55s after the discriminator/cache reconciliation. Receipt combined-codec-boundary.log. This resolves the identified cross-boundary risk; it is not installed/native acceptance.

## Remaining before activation

Worker finishes remaining native commit test caller fixes and final source handoff. Parent reviews final combined source/codec changes and their focused tests, pins merged core into paired Toad, builds installed candidate, checks delivery/queue UI and one fresh real configured-provider queue/compaction run because input locking/admission changed. real_queue_acceptance.py is prepared with typed documents but has not run for R3. Preserve UNKNOWN, native identities and exact once/order; never reconstruct queue authority from saved observations. Then merge paired pins and activate only intended idle owners through normal restart.

R6 implementation continues independently with Pascal; R7 remains queued. CI deferred.
