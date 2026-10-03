# Core569 ready

Source qualified: 280e6948f56d653746f1a1d05a87742ac0caaf1e. Evidence-only commits after this have identical production bytes. Current main is already an ancestor; no integration conflict.

## What changed

The existing selected idle native SessionManager owns its loaded EntryStore. Both initial and exact retained-payload preparations now call its original prepareCompaction through NativeQuery. They no longer launch two detached full-history indexes. The two preparations still check different payload budgets. Source capture, currentness, native writer retirement, admission and UNKNOWN custody remain with their original owners. Offline callers without a selected child keep the existing standalone helper; it is not a fallback for failed selected custody.

PiRpcChannel owns the one 16KiB observation response-frame limit. NativeQuery carries strict/bounded receive options through the original channel and JsonlStreamReader. Repeated selected-route raw-length/decode/read/caller limits are deleted. This response-frame limit is distinct from retained-payload budget.

Cheap settings retain their original 3s default/5s maximum. Whole-history preparation derives its deadline from the existing PrepareCompactionHelper/PiHelper 10s operation budget. The original helper also uses that declaration, replacing its literal. No settings deadline is applied to prepareCompaction.

The native header locator is NativeSessionIdentity.locate, not read. All production and test locator callers migrated. NativeForkCreation keeps its original bases and now inherits TypedRow.read through TypedTable. No alias, SQL adapter, base-order workaround or changed durable row format. Before/after AST covers all identity descendants (NativeForkCreation, NativeWitness); the installed identity/table members have no remaining non-generated name collision. Original fork readers include NativeEvidenceRead.inherited_source_prefix, JournalTable.for_session, SessionJournalHistory.exists and recorded retention/continuation readers.

Production delta against merged568 base0608319c: **13 files, 204 added / 87 deleted**, excluding the two-line native pins update, tests and evidence. The deleted selected helper preparations, copied transport boundary and colliding locator name are replaced in all their callers.

## Actual installed qualification

Reused holder: `.artifacts/runtime-scoped-input534`; Python3.14.2, SDK0.12.1. Normal wheel build and declared `[acp]` installation; pip check passed. `final-installed-source-proof.json` records exact source/wheel/installed equality for all342 package members, including declaration-owned packaged assets and native pins. No application PYTHONPATH overlay.

Native915 artifact from Sch2ba25210672e2f533acafd412ebd3ed979d573f1:

- Package `/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-915c78ab702bd8ea/node_modules/@earendil-works/pi-coding-agent`.
- Manifest `915c78ab702bd8ea66500e64ee64d1d623e3e6ee0702831166e6b6be3788391b`.
- Full tree `b678e1f41c4cc2bde46a65fa71c803306b86618103031ca029db44cb9a690339`.
- Original stock recipe, import inventory and full trust in `artifact/`; only RPC bundle changed from044. No rebuild after locator rename.

`configured02/owner-terminal.json` and `postexit.json`:

- Actual 42,116,826-byte SDK saved fork, configured openai-codex/gpt-6.1-sol HIGH, actual native startup and read-only dry queries.
- Initial preparation **0.171429513s**; exact retained-text preparation **0.156082342s**. Same acquired child, ready cut/leaf/revision, 40,344 estimated input tokens.
- Original settings: context272000, reserve16384, keepRecent20000, taskAware false. Original fact projection found zero retained task facts; the exact54-byte empty retained payload was used, not fabricated facts.
- Driver exit0; native retirement and runtime shutdown complete. Both exact process identities absent, both groups empty, InputDocument zero rows. Original source SHA e864648e12beb2e7a376b447783235dc6149ed3d34b817e6bebd3b89ea440770 and proof31339d72123405bd5015f48de43c10fec7fbdbe92ed61589cda18ae626055fcf unchanged.
- No prompt, provider request, compaction write, replay, public mutation or UI claim.

`original-fork-sql.json`: actual Arendt original three-cut journal, installed NativeForkCreation.one/for_session real9618-entry fork row and empty SELECT pass through TypedRow.read. Journal SHA5f33a71c3ab7d24d31576c0d1dc49684745867c03d5155ee9f6d059b8911294f unchanged. This exercises the production reader family, not a synthetic table.

Previously completed controls remain unchanged: selected native initial/retained query and cancelled receipt/child retirement, local actual native protocol/provider fixture **10.46s**; four native writer join/cancel controls **0.78s**. Logs retained, no repeated unchanged controls.

## Preserved negative and limits

The first42MB run completed both preparations (.183/.166s), then its interactive harness attempted epoll on /dev/null and stalled. Raw log/receipt remain unchanged. Exact owned SIGINT caused exit130; separate postexit receipt proves both identities/groups absent and original bytes unchanged. Dry-only mode now closes directly; interactive physical-client mode still waits for its original stdin lifetime.

This proves installed preparation and fork-source reader closure. It does not establish the old13.562s finish-to-next-request gap is fixed, nor the98.141s configured summary duration. Those remain distinct latency work. No new paid reproduction was used.
