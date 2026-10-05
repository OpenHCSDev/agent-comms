# Same configured02 source/request read preparation

Source trace: `RecordedNativeProbe.capture_input` preserves the passed original
checkpoint, its original registry FileProvenance and private wire path. The
three-cut producer captures registry immediately before compaction and retains
the canonical private USER wire. Its condition-application path passes the
completed cut unchanged into this capture. The previously accepted read's
published preimages include the cut-3 registry and source wire, the journal,
original parent and child SDK/native evidence, sealed manifest and all segment
serializations. Actual availability is not inferred from this source trace.

Use the already sealed configured02-read01 postterminal probe (hash1d2aec),
not old appendable probe0c519 or today's registry/preview. Existing
RecordedNativeCheckpoint.read_record, ReviewedArtifact and InstalledSource own
all byte/origin checks. Existing RecordedNativeProbe.observe owns the complete
journal/input/request/source read; no new reader/scanner/framework or source
reconstruction. New 669/670 fields are published only where original evidence
supports them. Missing originals remain unavailable or refuse through those
owners, not converted to positive evidence. Output is a separate measurement;
no original probe/reference/input/state is changed.

`command.json` is the exact proposed command/cwd/environment/output tuple. The
literal Python command was parsed without executing/importing it. No holder or
original artifact was read during preparation. The old configured02-read01 raw
readback supplies protected preimage hashes. This request needs one NEW fresh
Bohr recorded READ/import purpose; old540 purposes are closed. No native artifact
or SDK/provider/model input is needed, and no Sch artifact loan is requested.
The original656 installed product remains its truthful old filewheel source;
it is not declared equal to current main. New private reader bytes are separately
pinned in the command. All current runtime/native/tool owners remain untouched.
USD75/30-pair study is still unapproved.
