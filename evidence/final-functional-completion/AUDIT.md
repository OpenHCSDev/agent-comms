# Final live functional acceptance — PR17 / PR94 / PR95

2026-09-28 UTC. Owner: parent Codex thread.

## Verdict

The six required operational outcomes are implemented, merged and installed.
This audit inspected current installed source, live coordinator SQL, retained
native session entries and commit journals, actual-provider receipts, migration
comparisons, mounted UI results and fork merge state. It also ran twelve focused
checks against the installed package. No CI gate, new provider call, historical
input replay or live data rewrite was used to reach this verdict.

Current installation: `runtime-original-closure-20260928`, core206
(`f7716d542135bc8a41833561c05e2170aa166ac8`), Toad100
(`8adfcad0302ae1ffd6ab5074486cf0a5cf8c24e1`), Textual
`4fa6a9c440eaaa7dfaad45a33af146fc4b7e922e`. Pins and prior installed acceptance
are merged in core207. The normal five console links select this runtime;
`stack/bin/toad-comms` remains the normal launcher. The two intended owners are
alive, locally attached, idle and configured for their existing model. The
active bus/checkpoint is at62 with103 identities. `installed-observation.json`
is the final observation, not an assertion that all restored identities run.

The earlier independent functional audit's only outstanding installation action,
restart188, is closed: the correction was installed and exercised, including
normal replacement of both owners during the latest activation. See
`../owner-restart/live-activation.json` and
`../original-caller-integration/live-activation.json`.

## Requirement-by-requirement evidence

Paths beginning `../` are relative to this directory. `STATE` means
`/home/ts/.local/state/agent-comms`. Every row is owned by parent integration;
no implementation or activation action remains for these six rows.

| Requirement | Inspected evidence | Result and scope |
| --- | --- | --- |
| 1. Automatic human and agent channel relevance; IGNORE/ENGAGE; passive/direct routing; configured context/model | Live `coordination.sqlite3`, captured in `receipt-observation.json`: source23 human and25 agent each have an engaged recipient with distinct TRIAGE/FULL inputs and an ignored recipient with TRIAGE only. Source10 has two passive deliveries and zero native inputs; mentioned12 has one completed FULL. Latest unmentioned61 has the same two-subscriber split and reply62. Actual receipts: STATE `admission-human-live-response.json`, `admission-agent-live-response.json`, `native-recovery-fresh-message.json`, `current-context-live-message.json`. Current `wake_injection.py` includes registered current goal/task rather than stale bootstrap alone; installed owners retain configured models. Five current installed ordinary-send cases additionally cover direct, mention, ignore, engage and addressed-to-other behavior with fake model output. | Automatic delivery and useful selected execution work for running subscribed owners. IGNORE does not launch FULL. Stopped historical identities remain stopped; no automatic old-work replay. Local fake cases are contract checks, not additional actual-provider evidence. |
| 2. Integrated history, identities, metadata, saved sessions, lossless migration | `../functional-audit/source-inventory.json`:8420 preserved rows, including242 original #nra rows; #openhcs metadata exists but preserved sources contain zero #openhcs posts. `saved-session-pages.json`:88/88 referenced native files read successfully, including available earlier pages. Latest `../r6-integration/live-history-ui.json`: mounted normal #comms/#nra,111 saved choices and original UX transcript. `route-migration-corrected.json`:59465 original routes,5617 input displays,5408 routing rows; `attached-route-migration.json`:17502 attached routes; all comparisons equal. `../r3-integration/saved-inputs-final.json`:7780 saved inputs,96 cursors,2580 UNKNOWN records preserved. | History is reachable through normal channel/session UI and pagination with original provenance. Copies were used for migration comparisons; successful live activation used the tested migration after old writers exited. Missing #openhcs messages were not invented. Readability does not authorize replay of old incarnations or uncertain work. |
| 3. Normal launcher, visible feedback, latency, stable truthful labels and failures | STATE `admission-ui-timing/result.json`: mounted submit feedback0.098s, own message0.965s, response9.803s. Latest actual live61→62 read/bash16.505s in `../r6-integration/live-response.json`. Current normal launcher opens UX, renders owner, no import/traceback, inner exit0: `../original-caller-integration/launcher-summary.json`. Native-history contention retains the last valid observation; selected-source labels distinguish partial proof from complete history. Current failure tests in combined57 cover terminal Error publication and cleanup. | Measured immediate visible feedback and completed responses, not a universal4–5s or p99 claim. Latest coding latency includes model/tool work. The existing original source3 native-proof gap remains visible and truthful. Mounted tests are not an X11 rendering benchmark. |
| 4. Ordinary FULL coding and cooperative claims; ACP retained | `../functional-audit/existing-coding-transcript.json` matches actual configured-provider read/edit/write/bash calls to four non-error tool results from `../channel-coding/installed-coding-20260928-result.json`. Current `../original-caller-integration/combined-seams.log`:57 cases, including actual pinned native tools against a local deterministic provider, selected lease/claim release, terminal feedback and TurnRunner. Latest actual live61 uses read/bash. Installed current-delivery owner/queue pilot passed. | FULL provides normal create/edit/read/shell work. Claims gate mediated edits/writes and release the matching ownership; they are cooperative coordination, not a universal shell or external-editor sandbox. ACP and queue paths remain working. |
| 5. Merge, install, activation and resource discipline | Core206 and Toad100 merged; stack pins/lock resolved and merged207. Final observer checks actual executable paths, owner liveness/local participation/configuration and checkpoint equality. Latest activation preserves bus62/roster103 and succeeds without the old restart recovery. Tests below import the current installed package, not an uninstalled candidate. | All required operational fixes are installed. User worktrees and shared changes are preserved. Only current/previous runtimes retained after reference checks; owned fixture copies removed. Final resource sample:9.6GiB free on `/`,37GiB on `/home`,25GiB available RAM,33MiB swap used. This is a sample, not a perpetual monitoring guarantee. |
| 6. Default automatic adaptive compaction, configured route, continued sessions, commit linkage, queue, cancellation/UNKNOWN | Installed `CommsAgent` default is true and no owner/global setting disables compaction. STATE `pr95-real-summary-plan-20260928.json`:six real ACP turns, three compactions, four facts retained across both recall responses. This audit joins each selected summary to its committed operation and actual native compaction entry. Latest `../r6-integration/installed-provider-queue.json`: durable accepted-not-started followup during summary; committed compaction at native position7 then original8/followup10 once/in order; expected original reply and all four followup facts. Live journal has one linked summary/committed operation with no remaining attempt. Current installed cancellation/UNKNOWN checks pass. | Production adaptive path is enabled for eligible native sessions, including owners without goals. Native bundle/preparation, selected summary, journal commit, continued-session and original/future input paths are integrated. Trusted thresholds were lowered only in owned acceptance projects to trigger the real mechanism; usage/model outputs were not fabricated. |

## Current checks and reproducibility

- `inspect_receipts.py` runs with installed Python, reads live SQL in read-only
  mode, joins real journals to native session entries, validates retained facts,
  source-stage/disposition relationships, coding results and migration receipts.
  It writes only this owned evidence directory. Result: `receipt-observation.json`.
- Twelve installed-package checks passed in6.42s (`installed-fences.log`):
  `test_compaction_send_admission.py` (2), journal UNKNOWN reconciliation (1),
  stalled publication deadline/cancel/partial delivery (3), actual child/watchdog
  cancellation cleanup (1), ordinary send direct/mention/ignore/engage/observer (5).
  Command: system Python pytest with `PYTHONPATH` set to the installed runtime's
  Python3.14 site-packages, `-o addopts='' -q`, and test temporaries under `~/wt`.
  No model API was called by these tests.
- Existing actual-provider tests were inspected, not rerun solely for completion
  wording. Current changes since the latest actual queue and channel checks are
  caller/constant ownership closure; their focused source/native and installed
  owner/queue/launcher paths were tested during activation.

## Preserved failures and explicit limits

The successful six-turn repeated-compaction project was subsequently used for a
queue test that exposed a real input-admission bug. Its additional reserved
summary attempt and uncertain inputs are still preserved. They are not counted
among the three successful compactions and were never replayed. InputDrain fixed
the defect; later fresh actual-provider queue runs passed, including the latest
installed R6/R7 run. The final journal observation records both facts.

Existing full-prefix native coverage remains blocked at original source3. A bus
checkpoint through62 proves committed bus integrity, not retroactive injection
of every original message. Current selected input/response proofs are separate.

This functional verdict does not assert the original universal module/function
size or lock-location guards, exhaustive archived-stream replay, or50/100/150
thread p99 benchmarks. Those original refactor qualifications remain in the
original-plan audit. C0 and named ownership replacements, including residual
caller deletion, are installed; five PF1–PF5 proposals remain unimplemented for
the next refactoring plan.

The proposal explicitly labels owner-decision ingestion, arbitrary DM obligations,
supersession and task-to-commit heads as future scope. Manual canonical-native
`/compact` still refuses until journal-aware manual admission exists; automatic
adaptive compaction is live. Issue107 remains the requested open design issue
for non-hardcoded native proof-journal recovery. Open PR108 is a separate dormant
advisory-only claim observer, not the mediated write authority; CI-only114/115
remain deferred by the owner. None is silently called implemented here.

## Handoff

No active worker is needed for the completed six-outcome goal. Both assigned
refactor workers finished their source closures; PF1–PF5 are proposed planning
input, not running work. The authoritative current checklist is
`STATE/pr17-pr94-completion.md`; its prior contradictory progress sections are
preserved in a dated archive rather than deleted.
