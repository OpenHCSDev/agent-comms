# Install merged incremental unread indexing and visible pending state

Core249 and Toad112 are merged to main. This paired stack pins core1b98037,
Toad60a87e0 and Textual16ede00, installed into the fresh immutable runtime
~/.local/share/agent-comms/runtime-unread-20260928.

One live activation correction derives the disposable index filename from its
existing schema version. The existing open UI can continue with its existing
cache while a fresh UI uses v3. No alternate reader, history converter or runtime
flag was added. Do not delete the old cache while its UI process is alive; it is
queued for cleanup after detach. Native transcripts/read_ledger.json are unchanged.

17 local unread tests pass, including an actual old SQLite write transaction
held open while the new index computes successfully. Local debt ratchet has
zero increases; scoped Ruff and diff check pass. Initial test invocation had
only fixture setup errors because the owned .artifacts parent was absent;
created it and reran all17 successfully.

Actual installed wheel UI acceptance on copied current+archived state renders
#comms, agent-comms-ux and pr95-selected-pi-summary-owner. No messages sent;
watcher/child joined and interpreter exited0. This uses the same real retained
native sessions and project directory as the user, while copied bus/certificate
inodes are rebound in the isolated owned probe. Original files remain unchanged.
The running user UI535509 was observed alive throughout.

Activation selects this runtime through existing normal launcher symlinks.
Owner processes need no restart: this change affects view indexing/presentation,
not native execution or compaction. Existing UI imports update on its next launch.
No UNKNOWN input is replayed or reclassified. Compaction242 remains installed;
systemic native history243/proof244 acceptance is separate ongoing work.

Known separate watcher teardown BrokenPipe reported by112's synthetic native
UI pilot remains owned by Copernicus; this retained archival check exits cleanly.
