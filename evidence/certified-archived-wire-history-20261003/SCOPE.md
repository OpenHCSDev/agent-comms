# Original archived full-history boundary

Current source7926d83f. Actual ordinary installed CLI full #openhcs history fails
only after current-page traversal enters an archived snapshot. Original traceback:
parent432 .release-private/live-openhcs-558354-20261003/full-history-failure-traceback.txt.

HistoryArchive.page passes the immutable source to live WireLog/MessagePageRequest.
_store_lock constructs another live WireLog and its current checkpoint schema
qualification rejects the archive's original checkpoint before reading messages.
Current notifications and ordinary history window succeed; this is not current
bus failure or proof of import registration trouble.

Trace the original HistorySource/ArchivedAccess provenance and byte certificate,
archived page readers/native source consumers, and current mutable checkpoint
publication as separate existing owner behaviors. Preserve archived source bytes,
original certificate/provenance and current delivery/source guards. No immutable
archive rebuild, legacy runtime decoder, skipped validation or error masking.

Mendel owns this family. Same existing isolated checkout reused after publishing
555 and558; no new WT/environment/native copy. Existing NRA AST first; coherent
owner/consumer source change; affected actual installed full-history read last,
no public write/input/replay/provider. Parent owns live publication and555 receiver.
