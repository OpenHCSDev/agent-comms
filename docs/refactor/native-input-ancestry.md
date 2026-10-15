# Native input ancestry: owned source pass

Implementation owner: core-retry. Integration/observation/UI owner: codex.
Base: Core173d2bfd, paired Toad pinned in native-ancestry-8302-before.json.
Scratch owner/purpose: core-retry, saved-page measurements and disposable fixtures,
`/home/ts/.cache/agent-scratch/core-native-ancestry-8302`.

## Required answers and source

- Parent/entry identity belongs to `NativeEntryCoordinates`, including opaque
  entries through `source_coordinates`; display fields cannot redefine it.
- A user boundary belongs to the declared NativeEntry/PiMessage behavior. Stop
  at the actual parent-linked input, never a physically nearby unrelated user.
- Original native input/stage/publication belongs to NativeRuntimeInput's exact
  file + session + user-entry relation. Copies/inherited files cannot borrow it.
- `NativeTranscript.reading` owns one original descriptor across a page's header,
  records and ancestry. Replacement cannot splice ancestors from another inode.
- Full message bodies belong to their original decoded NativeRecord. Reuse the
  contiguous fragment's originals; older ancestry needs coordinates, not tool
  payload decoding. Decode an actual matching user through the existing codec.

Existing refactor-audit parser:607 modules, zero omissions,895 family sites.
This is syntax/name evidence, not binding or behavioral proof. SQL/callback and
subclass dispatch are checked semantically. Relevant patterns: BOUND-1/2 and
IMPL-12; share boundary/resource behavior rather than another cache/index/codec.
NativeTranscript.inputs_for is also used by strict publication_input; both
must preserve parent chains, roots and untracked user boundaries.

## Required finish line

A bounded page must not depend on lifetime-history length. Coordinate-only
backward traversal is an intermediate checkpoint, not completion. The existing
native EntryStore owns seekable parent/offset selectors, but DiskEntryStore's
SQLite is per-open and unlinked. Core cannot acquire that index; reconstructing
it on every page would still be linear. Extend that original source/index
lifetime, derive each entry's input ancestor from its validated parent during
index/append, and consume that same selector from Core. Untracked user entries
are boundaries too. No second Core index/cache and no nearest-user heuristic.

Toad-close-lifecycle confirmed it owns only certified-bus context-manifest
paging, not NativeTranscript, EntryStore, native indexes or acquisition. These
native changes remain with core-retry; codex retains bus-lock/observation work.

## Change and acceptance

Keep source traversal in NativeTranscript and its acquired-resource subtype;
project ancestry through NativeEntryCoordinates and declaration-owned native
message behavior. Delete the full-body ancestor walk and path reopenings inside
an acquired page. Keep actual source branches and inherited-session identities.
No metadata side index, retained ancestry cache, raw role switch or format fallback.

Codex's real saved page:4 events,486 NativeRecord decodes,139ms in inputs_for
of234ms profiled read. Preserve the exact returned page/provenance; compare
that actual saved-source path after the coherent implementation. Then confirm
branches, off-page intermediates, opaque entries, incomplete tails, replacement,
forward/reverse consumption, publication and absence of SQL custody during walks.

PR872 is an unfinished alternate checkpoint; core-route-counts confirmed no
active overlap. It detached unresolved off-page chains and did not complete
ancestry/live acceptance. It is not adopted as proof or implementation.
Two old observer fixtures in test_viewer_snapshot_index still reference the
removed ObserveSidebar.apply API; core-retry retains their migration, separately
from production observer orchestration owned by codex. Codex explicitly deferred
sidebar rechecks in favor of this native source work.

## Intermediate checked checkpoint (not ready)

NativeEntryCoordinates owns parent identity including opaque extension entries.
Original fragment records are reused; older ancestors project through that
existing schema and native-message declarations, not a second codec.
PiPayload.wire_type owns shared object/variant validation. Acquired native
readers inherit traversal against one descriptor, including header/ancestry;
forward cursors remain stable during nested reverse reads.

ReadTranscript on the real configured saved toad-close-lifecycle source:
34 events under a retained native/wire bound, identical complete-page SHA256
before/after. Three profiled baseline reads:393.9/271.3/274.8ms; intermediate:
208.4/128.6/120.1ms. Full-body read calls726→34, but667 coordinate decodes and
three ancestry walks remain. This does not meet lifetime independence or50ms;
it is not installed/physical acceptance and does not attribute the5.623s gap.
Exact bound/identities/results: core-native-ancestry-8302/{request,before,after}.json.

Python family census:607 modules, zero parse omissions,895→915 syntax/name
sites, with the same pinned Toad3ad856b71 and metaclass-registry roots. Dynamic
factory/alias resolution remains manual; these are not binding or timing proofs.
Native JavaScript/index lifecycle is a separate required source pass before its
implementation. The intermediate Python debt ratchet reports two additional
foreign-absence syntax probes in native_transcript.py, not zero; no GodClass or
boolean-chain increase. The probes include typed presence and nullable parent
consumption in the still-to-be-deleted traversal. Ruff E/F/I and diff checks pass.
A new opaque native entry needs no ancestry roster edit: its declared original
coordinates suffice. A new declared Pi input-message role supplies its own user
capability; MessageEntry consumes that declaration, not another role-name list.

60 focused checks passed across the native boundary/context batch. Six stale
terminal fixtures use removed output/failure APIs; three stale cursor/proof
fixtures use removed assignment fields/old error wording. The isolated native
publication fixture reached a catalog EOF before its test turn; the reused
immutable package verifies, and the source-only runtime link was retired after
checking no native process used it. Retain its log/fixture for diagnosis, not
as an accepted native turn. A prior broader outbound batch also exposed five
notification/identity/read failures; their exact names/log are retained and they
have not been dispositioned by a baseline run. No production fallback was added. The strict corrupt-native test now distinguishes complete
corruption (raises) from an incomplete tail (no decoded entry).

The old verification UI on:215 was closed normally at codex's request:
st411658/toad411665/observer411741 gone. No native worker was signalled; original
sessions/auth/UNKNOWN remain untouched. Receipt:old-ui-retired.json in the
owned scratch root. Xvfb106063 is retained for the isolated acceptance display.
