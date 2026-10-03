# Channel and DM pages use the existing bounded reader

Parent owns this followup; Heisenberg owns IRC paint profiling. #588 stays scoped to captured display identity and the retry-loop deletion.

Source-first census at Core5025c4ba and normally joined ToAddeeafa32 uses existing refactor-audit Package/ParsedModule: 311 Core and289 ToAd production modules, zero parse omissions,55 selected declarations/imports/calls across9 Core files. Syntax does not prove dynamic alias/MRO resolution; the original owners and consumers were read semantically.

Actual #openhcs source reads returned30 rows in2.49–2.73 seconds because BusPresentation.channel_page collects the whole18MB bus. MessageBus.display_page and DM already use MessagePageRequest.read, with the original BusPageIndex and the canonical log fallback. The channel implementation bypasses that existing reader.

Use the same MessagePageRequest reader for both views. Keep identity/membership capture with DisplaySelection and its original document resources. Reading owns these resources on the existing type; snapshot, channel page and painted acknowledgement consume it. Release selection resources before bounded page preparation, as the existing DM path does. Genuine identity/projection changes still refuse stale paint acknowledgement. The canonical WireLog reader owns the actual opened source cut and original index/scan policy; do not add another cache, resource wrapper, codec or retry loop, or change the index's authority.

Implementation and final source/installed sanity checks follow this coherent owner-and-consumers migration. #588 installed qualification continues independently on its immutable source and wheel. This draft does not claim a speedup or live readiness yet.
