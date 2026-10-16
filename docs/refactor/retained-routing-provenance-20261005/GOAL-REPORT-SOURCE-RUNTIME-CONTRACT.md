# Current read protocol; old records have one carry

Claude12686 supersedes the shared old-producer projection in #971. Both ends of
normal acquisition use current declarations. CurrentTypedCapture has no source-
shape selector or generic old-producer base. CurrentThreadProjection acquires the
selection and RegistryDocument together; OriginalOwnerRead carries only those
current facts. The stdio boundary encodes the record once through FieldCodec.

Deleted: RetiredGoalReportProjection, its RetiredOwnerRead transport, the generic
OriginalTypedCapture source-shape API, the forwarding LiveOwnerReadProjection
layer, and both legacy fixture default modes. A request for the old projection
is rejected, not discovered from missing fields or adapted into the current
shape. There is no old-shape reader in src. Old source/control bytes and earlier
observations remain in Git, not an executable compatibility path.

RecordedOwnerRead separately owns current recorded acquisition and typed
FileRevision proofs. It encodes RegistryDocument through that owner's declaration;
there are no copied thread/alias grammars. Only codec-produced provenance is a
JSON value. JsonValue handling belongs to FieldCodec's existing JSON owner,
integrated from main #979; these tools add no workaround or second validator.

## Durable old records

The actual one-shot entrypoint is tools/cutover/restart_thread_format.py. Its
original-process kernel is restart_original_thread_format.py; the existing
ThreadRetirementCutover in thread_retirement_cutover.py validates complete
registry/release/history carriers, retains originals and uses OwnerCutover's
stopped-batch lifetime. GoalReportMemberRetirement belongs only to that tool,
not the current read protocol. This carry has not been executed or physically
qualified here. Codex owns reviewed execution/disposition and deletion of the
one-shot family afterwards. No conversion or install is authorized by this file.

A bounded shared-lock read of the live root's two authoritative Thread carriers
found zero last_goal_report_turn rows: 220 registry Threads plus 145 release
receipt Threads. This is a point-in-time read-only count of 365 carriers, not a
claim about frozen fixtures, external roots or historical wire. No wire/database
was opened. Receipt: .artifacts/current-only-capture/live-carrier-count.json.
Source originals, saved sessions, UNKNOWN inputs and completed effects remain
untouched. #958 and the separate route-identity/claim carries are not this work.
