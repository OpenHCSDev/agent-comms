# Source checkpoint

Source d24d3ba67fdee6f4c9f24a4be7bfe08304593c0b; Core551, not installed/Ready.
Five production files: 33 deleted lines, 97 added lines against main71e15bba.

Original manifest data remains solely in bus.jsonl. ContextManifestSources stores
only its recorded ThreadIncarnation and original byte range in the existing
sealed checkpoint. ContextManifestWireObservation owns production of that row.
WireSourcePointer owns shared byte geometry/capture; delivery and observation
pointers inherit it. CertifiedSourceRead captures both families under the original
certificate and lends only captured bytes/root/pointers beyond that lifetime.
The old capture_deliveries API and every caller were replaced in place.

WireLog.context_manifests now selects those original pointers in physical wire
order, using original birth and retained rename names. Both ContextCliCommand and
installed Toad ContextInspection.read keep their existing API. The original wire
-> bus -> registry acquisition order remains, with decoding outside custody.
No widget changes, context manifest copies, native RPC, new database, in-memory
manifest cache, alternate reader, source rewrite or input replay.

Cold recovery had a hardcoded three-table cleanup. It now derives membership from
TypedTable.members_with(CheckpointTable), so prior observation rows are replaced
in the same transaction as the original full-source validation/certificate.
Every target schema/append/installation path already enumerates this family.

Before/after AST: 1,002 modules over Core src/tests/tools and installed Toad
production; zero parse omissions. After evidence has zero capture_deliveries
references. Receiver/MRO resolution remains conservative; native producer format
is unchanged. Installed Toad evidence is a dependency source inventory, not new
UI acceptance.

Five affected source controls passed in 1.70s, using the reused534 interpreter
and explicit source imports. The controls prevent:

- decoding unrelated wire rows: cProfile observes one selected decoder call and
  no _snapshot_records call for a bus with 12 messages and two owners' observations;
- loss or mixing of original history: append/silent sequence, cold reopen,
  rename/predecessor diff and reused-name/new-birth behavior;
- duplicate observation pointers after interrupted publication: real canonical
  recovery rebuilds all derived members, preserving original message sequences;
- accepting altered sealed index bytes: original certificate refuses the read;
- breaking inherited delivery capture or message-only crash recovery.

The initial serial check run exposed old fixture assumptions: comparing/indexing
the now-deferred page iterator and a pre-existing response signature missing
exact_target. The existing _page helper now materializes original decoded bytes
after custody, matching its callers' existing contract. No production fallback or
response-policy change was made; the unrelated stale response fixture is not
claimed passing. Pytest's repository addopts requested unavailable parallel and
coverage plugins; the bounded run uses -o addopts='' with no fleet.

Target certified schema digest:
c4ff566ee8f2c15407d486291971c9ef52efb99801150311a9f0fe40cd8ce8d2

Release must use the existing quiet checkpoint rebuild from the original writer
to the target installer. Wire/native/session/UNKNOWN data stay unchanged; previous
schema is rejected. Sch granted the exact existing tool family. The old
retained_index_writer StoreLock-as-integer handoff is deleted; the child receives
its original descriptor via argv/pass_fds. Both target subprocesses exclude
PYTHONPATH. checkpoint_schema derives membership through the same TypedTable owner
as the runtime reader. install_retained_index already validates that inherited
descriptor's exact lock inode and original root; its behavior stays unchanged.
Sch owns the publisher/carry lane. Public and frozen original artifacts remain
untouched; a private installed rebuild control belongs to final acceptance.

Installed original observation CLI/Tree acceptance remains pending Sch's release
of the reused normal holder after the critical550 publication. No new environment,
native copy, package overlay, provider call or public modification was performed.
This checkpoint removes source-proven work and makes no historical8–16s claim.
