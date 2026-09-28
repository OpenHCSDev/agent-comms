# R5 document owners — merged and live

Core195 (89314b0fe56de769cb956a63acd870e89e8dee43) and paired Toad95 (a4ca07a2f71a71e8483d8b19322ae16cdc2bbc97) are merged and installed in runtime-r5-documents-20260928.

-174 distinct focused core cases passed, including document publication failure, reader/writer locking, bounded activity decoding and actual local restart preserving runtime metadata and nonempty shared ledger. Core evidence/r5-documents/HANDOFF.md records limits/earlier failures.
- Parent saved-data comparison:2 current runtime metadata and2 latest activity records match old/new; ledger empty and historical snapshots lack these documents. This is limited real-data evidence; nonempty ledger values and rename/delete are covered by source fixtures. Owned copies removed after compact receipt retained.
- Source and installed Toad10000-record activity/profile and all session-sort criteria passed. Installed append/read median0.485ms in this sample. Paired Toad95 retains raw receipts; no production direct old store caller remained, one deleted Activity.to_wire test caller migrated to FieldCodec.
- Normal two-owner restart succeeded, both ready/alive/local.103 identities,58 bus rows and checkpoint through58 preserved. Configured owner models retained; automatic compaction overrides remain enabled/default.
- Normal toad-comms PTY opened history label without import/traceback errors; inner exit0 after bounded interrupt, outer124 intentional. No prompt/provider request or old uncertain input replayed.
- Tested shared stack pins copied after unrelated configuration comparison; previous files backed up. Existing catalog.json remains authoritative. R5 requires no saved-data format migration.
- Removed the unused74MB R4 runtime after process/launcher/runtime/registry reference checks. Current R5 and previous R2 retained.

R1 Pi payload closure and R3 typed input/delivery documents continue in the two existing workers. R6/R7 remain queued. CI deferred. Earlier configured-provider compaction/coding and installed R2 history receipts remain retained and are not claimed rerun by R5.
