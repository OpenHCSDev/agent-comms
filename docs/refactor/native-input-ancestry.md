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
from production observer orchestration owned by codex.
