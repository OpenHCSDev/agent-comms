# S7 WireLog / Publisher — completed source handoff

Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/184
Stable implementation: fa18b5461acf6e201478e45af30babafedfa7a59.
Main183 6d2ee0246c190eb2091a9ed61399182301a8b974 merged without conflicts;
PR181 manual-compaction behavior and pins preserved.
Tree: /home/ts/wt/comms-refactor-s7-wire-log-20260928
Branch: codex/refactor-s7-wire-log-20260928. Pascal owns source; parent owns
paired Toad migration and installation. No live writes/provider calls/helpers.

## Actual ownership and deletion

- wire_log.py / WireLog owns canonical JSONL path, metadata and sequence,
  durable append/rewrite, strict private rows/receipts, claim projection,
  opened-inode snapshots, lookup, fsync read barrier and checkpoint interaction.
  Existing lock policy, checkpoint store and wire codecs are reused.
- publisher.py / Publisher owns configuration, registry/catalog dependencies,
  routing/mention/audience decisions and ordinary/cohort/claim/keyed publication.
  Actual transaction bodies moved; no MessageBus/shared-self backpointer.
- message_bus.py retains delivery/read/history projections and existing indexes.
  It composes log + publisher without old method/path/flag forwarding.
- bus_durability.py deleted. store_files invokes WireLog's real read barrier;
  fabricated MessageBus.__new__ and partial object construction deleted.
- Old MessageBus persistence/publication methods deleted; also delete send,
  _next_seq, _load_log, _load_log_unlocked and _publish_legacy. Legacy publish
  transaction is in Publisher.publish, not a parallel implementation.
- MessageBus: 2209 -> 1042 lines. closure-audit.json confirms zero displaced
  methods left. No compatibility API, duplicate store, saved-data migration,
  native authority change or CI gate introduced.
- Whole core producer/consumer closure is migrated (31 source paths, listed
  in changed-source-paths.txt). ACP/InputDrain/TurnRunner changes are direct
  bus consumer replacements only; no compaction journal/provider/commit edits.

## Toad / parent integration

caller-map.json contains exact member and call replacements. Main examples:

- bus.publish* / initialize_private* -> bus.publisher.<same method>
- bus.latest_sequence / total_messages / full_history_snapshot /
  message_by_id / claim_projection / read_initial_cohort / read_keyed_response
  -> bus.log.<same method>
- bus._path -> bus.log.path; bus._load_log() -> bus.log.full_history()
- bus.send(message) -> bus.publisher.publish(message).message_id
- checkpoint functions receive bus.log, not bus; same on-disk checkpoint.
- canonical locks -> bus.log.locked(...), same arguments/order/read barrier.

Comms Messaging, HistoryViews, Transcripts and other current components already
use these owners. Parent supplies any Toad caller changes. MessageBus read/page/
delivery API and ReadLedger remain their actual owners, not aliases. Existing
page/route/activity indexes remain disposable projections of the canonical log.

## Acceptance and exact evidence

Existing integration venv, absolute PYTHONPATH=THIS_TREE/src, pytest -o addopts='',
no xdist. Generated fixture roots are owned, never worktrees in volatile storage.

- consumers.txt: 223 passed (declarations-derived producers, envelope/human ingress,
  multiprocessing/concurrency, operations, channels/display, history/index/read ledger).
- repair.txt: 144 passed, 1 skipped, 1 fixture failure. It covers declarations,
  full private checkpoint suite including 1000 rows / 8 MiB, keyed-response fencing.
  The failure passed MessageBus to the new WireLog checkpoint API; corrected.
- coordination.txt: 128 passed, including the corrected claim/keyed checkpoint
  case, cohorts, admissions, foreground/NK delivery and coordinated runtime.
  One stale fixture expected no file after cutover, superseded by merged PR176's
  empty checkpoint installation; corrected to assert empty log + checkpoint.
- final-seams.txt: bounded run exited124 AFTER 13 passing cases: both corrected
  failures, six page-index cases and five native-source certificate cases.
  This is partial evidence, not a complete suite pass.
- cursor-final.txt: all remaining five checkpoint-cursor integration tests pass
  in60.74s, including current-root and migrated-root1002-row/>8MiB sources,
  unproven source rejection, UNKNOWN/no-replay and checkpoint rollback denial.
- seams-retry.txt: earlier60s shard ended after47 passing ACP private delivery,
  input-disposition and selected-write cases (including second-process ACP),
  before scale cases. Not reported as a complete suite. Missing-file collection
  attempt is preserved separately in seams.txt; no tests ran in that attempt.
- awareness-final.txt:28 passed, passive channel awareness + normal coding tools.
- Lint I/F passed; source/test diff whitespace check passed. Raw pytest failure
  evidence is retained verbatim, including pytest's trailing whitespace.

No unresolved observed functional failure. No optional reruns needed. These are
local fake-native/protocol and subprocess checks, not installed/live/provider
acceptance. Parent owns installation and paired Toad acceptance.

## NRA coverage and proof limits

Exact successful full-context commands: nra-before-command.sh and
nra-after-command.sh. Final invocation (from this tree):

    timeout 165 /home/ts/code/projects/nominal-refactor-advisor/.venv/bin/python -m nominal_refactor_advisor src/agent_comms/message_bus.py src/agent_comms/wire_log.py src/agent_comms/publisher.py src/agent_comms/private_bus_checkpoint.py --context-root src/agent_comms --parse-workers 1 --analysis-workers 1 --no-cache --scan-budget-seconds 140 --json --json-payload loop

Both complete exact_compact_global scans:79 detectors,0 omitted; each reports
1 semantic_mirror_without_descent finding in counts_only payload. No zero-finding
or equivalence claim. This extraction/caller rewrite is authored source movement,
not NRA-native behavioral proof. Native behavior evidence is the tests above.

## Remaining scope / blockers

No source blocker. Parent merges with paired Toad and deploys. This closes the
assigned original S7 bus persistence/publication surface; no new unrelated
redesign is proposed. External saved wire/registry/history encodings preserved.

## Parent integration update

Parent reports paired Toad branch refactor/s7-bus-consumers-20260928: mounted
private human-ingress/UNKNOWN pilot,5 channel/history readers and sorting pass.
Parent retains rollout ownership. Owned completed worktree fixtures/caches were
removed (cleanup.txt); failed evidence and source retained.
