# Channel and DM pages use the existing bounded reader

Parent owns this followup; Heisenberg owns IRC paint profiling. #588 stays scoped to captured display identity and the retry-loop deletion.

Source-first census at Core5025c4ba and normally joined ToAddeeafa32 uses existing refactor-audit Package/ParsedModule: 311 Core and289 ToAd production modules, zero parse omissions,55 selected declarations/imports/calls across9 Core files. Syntax does not prove dynamic alias/MRO resolution; the original owners and consumers were read semantically.

Actual #openhcs source reads returned30 rows in2.49–2.73 seconds because BusPresentation.channel_page collects the whole18MB bus. MessageBus.display_page and DM already use MessagePageRequest.read, with the original BusPageIndex and the canonical log fallback. The channel implementation bypasses that existing reader.

Use the same MessagePageRequest reader for both views. Keep identity/membership capture with DisplaySelection and its original document resources. Reading owns these resources on the existing type; snapshot, channel page and painted acknowledgement consume it. Release selection resources before bounded page preparation, as the existing DM path does. Genuine identity/projection changes still refuse stale paint acknowledgement. The canonical WireLog reader owns the actual opened source cut and original index/scan policy; do not add another cache, resource wrapper, codec or retry loop, or change the index's authority.

Implementation and final source/installed sanity checks follow this coherent owner-and-consumers migration. #588 installed qualification continues independently on its immutable source and wheel. This draft does not claim a speedup or live readiness yet.


## Published owner closure and final source checks

Production is four existing modules, 128 added / 72 deleted relative to accepted
588. No new class, schema, cache or reader. DisplaySelection.reading owns one
original registry/catalog/read-ledger acquisition, shared by snapshot, page and
painted acknowledgement. Read-ledger custody ends before yielding because paint
may write that original ledger.

WireLog.page_snapshot owns the opened metadata/revision/descriptor/record cut;
_record_snapshot forwards that same resource. Channel membership is captured
AFTER opening that cut. Both DM and channel use MessagePageRequest.read_opened.
BusPageSource.covers lends verified offsets into that original descriptor, with
page offsets and record reads bounded by its original byte extent. A SQLite read
transaction pins the original index row/offset set while the index may rebuild.
An unusable disposable index falls back to records from that same opened cut.
No second source open or fresh marker replaces the captured cut.

The first batch found a genuine membership-before-source race. Opening the
source before selection fixes it at the existing reader owners. Original race
controls now observe _opened_wire_snapshot, where both reader paths acquire the
source. The concurrent Mark Read control starts its writer only on that first
acquisition; otherwise observing the writer's verified source recursively starts
another writer. Assertions are unchanged and all failed raw logs remain.

Final batch: 22 passed, 2 existing activity-clock assertions deselected, 11.72s.
Those two unchanged assertions were also failing against the installed baseline
in 588; no blanket correctness claim. Shared DM painted-ACK, alias/retag/source
bounds and original race controls are included. Production was reasoned and
implemented before this sanity batch.

Final actual-bus channel_page source reads return the same 30 message IDs and
seq485..514 in 52.96ms first and 16.45/20.26/22.27/26.99ms afterwards. Original
whole-bus source reads at 588 were 2.49–2.73s. The earlier 744a6c19 timings remain
separately labelled, and the current index is already warm. This is source
against the real bus, not an installed UI/frame-time or controlled cold gain.
The failed final timing driver queried CoordinationSnapshot.messages instead of
the existing channel_page API; its negative is retained and production unchanged.
No inputs, native launches, provider calls or read acknowledgements were made.

Before/after census uses existing refactor-audit Package/ParsedModule and reads
311 Core + 289 Toad modules with zero omissions. Lexical owner/import/call sites
are recorded; generic method names can include unrelated receivers, and dynamic
alias/MRO resolution is not proven by AST. No new scanner/authority registry.

Next: one affected installed IRC/DM App journey on a released existing holder,
then readiness. The current default 390 release is independent and already live.
