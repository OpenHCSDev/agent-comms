# Original385: scoped native input corroboration

Source observed: installed Core5c55d1cc / Nativead533, original message385
`1ec243f17eac`. Parent original send/claim/history receipts remain unchanged.

The read-only join contains20 originalNativeRuntimeInput identities,10 original
turn-lease request streams and10 saved native journals. Each journal's captured
prefix remained unchanged while read (423,849,966 bytes total). This is running
source inventory, not stopped custody. No input, provider, owner or public store
was changed. Raw join and file hashes:
`/home/ts/.cache/agent-scratch/mendel-live385-latency-20261002/joined.json`.

Native FULL requests finished at24.631–33.840 seconds after the original wire.
The existing acquisition diagnostic (emitted after tracked exchange and resource
retirement, before admission.verify) was recorded2.234–14.243 seconds later.
The remaining interval to canonical publication receipt was1.432–11.663 seconds.
These intervals include several owners and are not per-function measurements.
No bus-exclusive-lock or provider blame follows from them.

Concrete source gap: every production NativeEvidenceScope consumer corroborates
tracked inputs, but its for_source factory opens the full history decoder.
HistoricalNativeInput, SourceCoverage and NativeSourceCursor therefore bypass
the existing NativeInputEvidenceRead projection introduced in530. The existing
scope must acquire that reader. Preserve original byte/hash/append/refusal and
descriptor lifetime, original SQL/input binding, UNKNOWN and current-owner fences.
No new type, cache, estimate, store, format or native artifact is required.

Whole production consumers: historical_native_inputs.py; proven_source_coverage.py;
native_source_cursor.py. Rendering and retained task readers acquire full native
evidence independently and are not consumers of this scope. Existing NRA AST
parser read all311 Core production modules at5c55d1cc: zero parse failures or
enumeration omissions.82 nominal references in source-consumers.json. Python
AST does not resolve dynamic receivers or parse native JavaScript/dependencies;
their relevant original source was inspected separately.

Arendt owns532/cold native restore and initial get_state. This change owns only
NativeEvidenceScope in native_entries.py. S3527 remains parked. Cursor work can
delay retirement/next delivery; this deletion is not proof of the cause of385's
pre-publication gap. Existing qualified native07 provider gate will not repeat.
Final checks will cover changed resource refusal/lifetime and one installed
saved original corroboration, with no new provider turn.
