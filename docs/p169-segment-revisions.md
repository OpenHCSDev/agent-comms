# P169: coordination revisions in the existing native context

Owner core-read-locks, issue895, branch fix/p169-segment-revisions.
No failed/UNKNOWN input replay, second context cache, reminder store or timer.

Source owner pass: ContextSegment/SegmentManifest already declare exact source
and digest; the native context already carries the received contribution tree.
NativeChildState owns whether a loaded child continued the previous turn or
lost that continuity through preparation, compaction, resume or project change.
Reuse those facts, not a second remembered-revisions dictionary.

Baseline/acceptance use a separate native fork of the immutable completed p35
cut supplied by toad-theme (94,566,492 bytes/13,003 records, assistant stop).
Never use core-awareness-index: it has pending input. The loaned p21 root,
viewer, original SDK/wire and inputs remain read-only and untouched.

Current main's new Core instructions already omit the historical #753/#754/#749
examples, and CoordinationSegment already filters peers through AlwaysListed.
Retained historical instructions are evidence, not newly injected doctrine.
Only actual current injected instruction files may be changed; project owner
instructions (.pi/APPEND_SYSTEM.md, AGENTS.md) remain untouched.

Checkpoint, 09:49: InstructionSegment now owns instruction/provenance construction
throughout the family. LoadedNative represents an opened/reset native context;
RetainedNative selects from actual native contribution manifests after an input.
Native preparation and mutating queries reset continuity. User input always stays.

Private-fork byte assembly measurements (not delivered-model receipt acceptance):
- Direct `hi`: coordination429 + input2 + unavailable-awareness71 =502 bytes;
  matching-received-manifest projection2 bytes; first/reset remains502.
- Collaboration: coordination597 + input34 + relationships590 + unavailable71
  + partner-activity528 =1820 bytes; matching-received projection34;
  first/reset remains1820.
No source-root writes, saved-input replay or provider submission occurred.

Existing native lifecycle checks:7 passed against the immutable native bundle.
Other existing checks:22 passed/7 skipped/20 failed, including absent old APIs,
installed-only assertions and missing native-package setup; this is not a green
batch. A native-inspection module also fails collection on a default fixture
argument. No tests added. Actual retained contribution receipt and installed
acceptance remain unfinished; do not treat these projected bytes as delivery.
Scratch/evidence owner: core-read-locks,
/home/ts/.cache/agent-scratch/p169-core-read-locks.
