# Inherited fixture and package remainder

Parent accepted PR255 head1a5c2522 and integrated it into229e6d7fe08. Owner explicitly removed full old-suite completion as a merge/deployment gate; no CI wait or deployment hold is imposed here.

| Item | Actual state | Owner / next action |
| --- | --- | --- |
| `test_selected_tool_native_package.py::test_actual_pi_loader_admits_packaged_channel_tool_only[True/False]` | Both fail on prepared Darwin0d7ebb4f package: old flat selected tool request722d, current broker/source requires nested request361b. Guard correctly refuses. | Darwin: rebuild/pin current source, actual loader and selected write proof. Exact source delta sent on Toad119. |
| Full combined old suite | No complete passing result claimed. Bounded remainder batches are receipts, not an aggregate full pass; later parent deletions changed collection. | Continue only concrete meaningful failures; does not hold deployment. |
| Mounted Toad opt-ins |50 cases skipped in remainder-thirteen because mounted pilot opt-ins were absent. No mounted acceptance claimed from that run. | Parent/Copernicus current paired installed acceptance. T2 requires its own affected real ACP/UI proof. |
| Synthetic selected-compaction private-session variants |4 cases intentionally skip: private-session evidence requires the real SDK host. All4 corresponding actual-host private-session failures were fixed and passed in44-case batch. | No blocker; retain actual-host coverage. |
| Passive-awareness11 historical failures | Parent256 deletes old production mechanism and tests. Integratedc3e7252a. | Closed by Cicero; never recreate ledger initializer. |
| Alias, split saved-view, old public drain, stock manual writer tests | Removed with production owners or by PR255; current canonical behavior retained and tested. | Closed; never restore old APIs to satisfy them. |

Evidence: `evidence/full-suite-closure/HANDOFF.md` contains exact commands/results and the interrupted fixture-lock attempt. Latest combined focused36passed16.04s. Other changed real/native/socket cases44passed43.46s and29passed2.88s. Earlier full collection had concrete failures now fixed/deleted; there is no known additional production failure beyond the package mismatch above.
