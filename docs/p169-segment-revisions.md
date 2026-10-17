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
