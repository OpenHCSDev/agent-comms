# Current read protocol; unused field migration removed

Both ends of normal acquisition use current declarations. CurrentTypedCapture has no source-
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

## No live field migration needed

The live root's two Thread files already use the current format: 220 registry
records and 145 release records, with no last_goal_report_turn member. The
installed reader accepts them, and the old tool refuses the current Thread
declaration before reading or writing a root. Stopping the fleet for that field
migration would do nothing useful.

The eight unused migration tools, their old-only fixture and their specific
tests are deleted. The current fixture now owns the same worker-ready helper
used by the current attachment/replacement journeys. Current and recorded reads
remain intact for #950. Earlier proposals and source/control bytes remain
historical evidence in Git and the owned evidence directories, not executable
migration instructions.

The count is point-in-time evidence about these 365 live records, not proof
about frozen fixtures, external roots or historical wire. Source originals,
saved sessions, UNKNOWN inputs and completed effects remain untouched. No
conversion, installation or process stop was performed. #958, the separate
route-identity/claim work and #978's old-worker launch change are not this field
migration.
