# L0B read-only closure audit — 2026-09-28

## Verdict

Production supports retiring the raw-text backend, but deleting only the early
return in `InputDrain.run_owned_input` does not close the mechanism. The managed
owner must have one validated native launch contract; remove the raw argv/stdout
execution path and migrate all capability checks and local-command callers.
Preserve native receipt, UNKNOWN/no-replay, cancellation, image, goal and ACP
behavior. No code, configuration, live state, process or owner worktree was changed
by this audit. Only this independent report and its lexical inventory were written.

The parent owns implementation. Source references below are observations in
`/home/ts/wt/comms-acp-saved-session-startup-20260928` during parent integration,
initially at `d0380c65e22e1f258eaae8e5f14271e58e77f829`. The parent subsequently
started integrating other branches; line numbers may move. This is not a claim
that an in-progress merged tree builds or passes tests. No tests or provider calls
were run for this read-only task.

## 1. Actual entrypoints and installed configuration

* `stack/bin/toad-comms:18-53` resolves the installed ACP runtime and launches
  `toad acp "$python -m agent_comms.acp"`; it clears inherited root/private pins.
  `/home/ts/.local/bin/agent-comms-acp` resolves into
  `/home/ts/.local/share/agent-comms/runtime-acp-extensions-20260928/bin/`.
* Read-only active route inspection found
  `~/.local/state/agent-comms/active-route.json`: root
  `/var/tmp/agent-comms-live-20260927-wzjtqhza`, root ID
  `e206f3766e60451a989ca34df0e2a94b`, native package
  `/var/tmp/agent-comms-pi-native-extensions-20260928/node_modules/@earendil-works/pi-coding-agent`.
* `acp.main:778-824` creates an attachment-only `CommsClient`.
  `CommsAgent.prompt:296` forwards attached prompts through `RuntimeProxy`;
  `PromptRuntimeRequest.result:128-156` calls the actual owner's `agent.prompt`.
  The owned branch at `acp.py:342` calls `TurnRunner.prompt_owned:146`, then
  `InputDrain.run_owned_input:653` for direct text/image prompts. `@`/`#`/relay
  text goes to messaging; it is not raw backend stdin.
* `active_route.ActiveRoute.bind_owners:67`, `private_nk_from_environment`, and
  `OwnerLifecycle._launch_owner_unlocked:448-520` pin the private root/package.
  Default `pi` becomes the installed `pi-comms-native`; the owner subprocess is
  `python -m agent_comms.worker`, not `agent_comms.agent_loop`.
  `worker.run:12-26` constructs `CommsAgent` with that private launch contract.
* A read-only `/proc/*/environ` check (allowlisted fields only) found two workers
  on the actual live root: `agent-comms-ux` and
  `pr95-selected-pi-summary-owner`. Both had `AGENT_COMMS_AGENT_BIN` pointing to
  the installed `pi-comms-native` above and the matching reviewed package.
  One `/bin/echo` worker belonged to
  `~/wt/.r3doc/boundary/test_real_stdio_roundtrip0/wire`, an isolated test root.
  No production text worker was observed. This is a snapshot, not a claim that
  arbitrary future environment overrides cannot select text mode.
* `comms_send` is declared in `tools.py:852`; `_send:143` calls
  `Messaging.send_message:54`, then `Publisher.publish_ordinary` or claim-envelope
  publication, followed by optional candidate-index scheduling after commit.
  `InputDrain.drain_owned_inbox`/`_drain_private_if_changed:345` reach
  `CommsAgent._drain_private_nk:591`; that path accepts visible initial messages,
  selects sealed assignments from `MutationStore`, and executes through the
  coordinated native path. The owner runtime socket forwards the same ACP owner
  operations; it is not another raw-text transport.

## 2. What the raw-text path actually promises

`backend.rpc_args_for:359` decides solely from executable spelling: basename
starting with `pi` gets `--mode rpc`; everything else returns `None`. That is
mode selection, not native capability attestation or package verification.

`TurnSession.prepare_launch:1273` implements a concrete alternate contract:
`[agent_bin, *agent_args, task]`, stdin DEVNULL, stdout raw chunks; images fail.
`initialize_output:1435-1476` decodes stdout with replacement, emits `Chunk`, then
`Done` from exit status/stderr. This path has no native start receipt.

`run_owned_input:662-670` bypasses disposition creation and original owner input
metadata for that contract. `OwnedTurn.admit:76` refuses bus-origin inputs without
native proof; `OwnedTurn.admit:102` and `TurnRunner.set_goal:554` refuse goals.
It is therefore not feature-equivalent to native execution even today.

The command is configurable in `TurnRunner.__init__:110-117` through
`AGENT_COMMS_AGENT_BIN/ARGS` or explicit constructor arguments. Keep configurable
native launch arguments/model selection; remove interpretation of an arbitrary
executable as a second text engine. Reject unsupported configuration clearly at
the boundary. Do not create UNKNOWN receipts and then silently run an unproved
text process.

## 3. Exact closure and typed-owner path

1. **Parent L0B:** delete the early text return in `input_drain.py`; direct owner
   inputs must always take the existing disposition/original-input path.
2. **Coordinate S13 backend overlap:** delete the raw argv branch, image/text
   split and raw stdout loop in `backend.py`. Preserve missing executable,
   nonzero exit, provider failure, shutdown and actual Pi stream behavior.
   S13 owns process supervision (`AttachedChild`/`BoundedRun` through A12); do not
   add another subprocess wrapper or replace its active backend edits.
3. **Delete the classifier mechanism and close callers together.** Observed 21
   `rpc_args_for` name occurrences in seven production files:
   `backend.py` (definition, two discovery functions and launch), `input_drain.py`,
   `owned_turn.py` (admission, passive context, compaction, model/thinking args,
   persistent child), `turn_runner.py` (goals), `acp.py` (images/followups),
   `session_lifecycle.py` (auth, image/title/queue capability metadata), and
   `manual_compaction.py` (S9 validation). Replace mode tests with the single
   typed native contract at construction. Preserve genuine optional controller,
   session, auth-client and runtime states; they are not text-backend compatibility.
4. **Reuse existing owners:** `PrivateNkLaunch` owns root/package selection;
   `NativePiRpcLaunch` (`native_pi.py:223`) already owns verified argv/cwd/env/session
   facts, with `prepare_native_pi_rpc_launch:461`. Its own documentation explicitly
   rejects basename inference as authority. Extend these current launch owners
   for managed ACP requirements where needed; route the resulting launch through
   `TurnSession` and S13's child capability. Keep package validation and per-input
   attestation distinct. Do not add `BackendKind`, a parallel capability registry,
   a `supports_native` boolean with duplicated branches, or a TextBackend subclass
   that merely preserves the retired second mechanism. A typed launch does not
   certify model receipt; existing native input/context proofs still do that.
5. **Close `agent-comms-agent`, not just ACP.** `pyproject.toml:56` still publishes
   `agent_comms.agent_loop:main`. `Participant._tick:125` ACKs before executing;
   `_ask_agent:151-174` directly invokes the shared backend. It is not the live
   worker path. `plans/s1-evidence/HANDOFF.md:30` explicitly retained this command
   in round 1. Round 2 permits removing the duplicate mechanism, but a supported
   command's behavior cannot be inferred dead just because no process is running.
   Recommended closure: make the supported headless command use the existing
   native owner/runtime lifecycle as its canonical implementation and delete
   `Participant`'s polling/ACK/backend loop, including its consumer if no callers
   remain. No delegating compatibility API remains. Check identity adoption,
   channel/DM routing and worktree semantics explicitly; the old test executes
   in the sender's worktree, whereas the owned path uses the owner's worktree.
   Record that semantic change in the closure receipt rather than silently porting
   the obsolete private-method assertions.
6. **Comms-owned Toad projection:** `emit_queue_state:167-191` publishes old `queue`
   and top-level `restored` arrays beside `queueBinding`/`queueState`. Remove those
   old projections. Installed Toad `toad/acp/agent.py:417` consumes `queueState`;
   `:502-509` still contains an old-`queue` branch. Delete that branch in the paired
   Toad change, keep the nominal queue view, and pin/install together. ACP's outer
   protocol is external; `agentComms` metadata is our own format.

## 4. Test deletion versus behavior retention

| Observed test/caller | Closure action |
|---|---|
| `test_backend.py::TestTextFallback::test_non_pi_backend_streams_raw_output` | Delete with the raw-text engine. |
| `TestRpcArgs::test_other_backends_stay_text` and basename-based Pi classification test | Delete obsolete classification expectations; test the actual native launch contract and invalid configuration instead. |
| `TestTextFallback::test_missing_backend_yields_done_not_ok`, `test_nonzero_exit_marks_not_ok` | Preserve failure behavior at the retained native launcher, not the deleted test class/raw shell contract. |
| `test_agent_loop.py` (eight tests; shell stubs/private `_tick`/`_ask_agent` calls) | Delete obsolete structure tests with Participant. Retain headless identity, routing, activity and process-failure behavior in a bounded owner/runtime integration test. Explicitly adjudicate sender-versus-owner worktree semantics above. |
| `test_agent_events.py` imports `ParticipantEventConsumer`, uses it in the new-case/MRO test, and names `_ask_agent` in source guards | Close these references if the participant consumer disappears. Preserve one new-case test through surviving production consumers. |
| `test_acp_input_disposition.py::test_direct_cannot_launch_text_backend_without_native_start_proof` | Retain the no-native-proof/no-launch/UNKNOWN behavior at the new admission boundary. Never delete the authority guarantee merely because the old fixture says text. |
| `test_prompt_queue.py::test_cancellation_restores_unprocessed_user_queue` | It asserts the old top-level `restored` array at line 96. Replace that assertion with retained typed queue-state/user-draft restoration behavior in the paired Comms/Toad path. |
| `test_turn_runner.py` / `test_runtime.py` / `test_acp.py::TestWireProtocol::test_real_stdio_roundtrip` | `/bin/echo` often supplies configuration only. Keep isolation, mounted cancellation, socket/stdio error propagation and relay tests. The stdio test sends `!relay`, so it is not proof that a model/raw-text turn is necessary. |
| Native input, image, goal-original-input, queue, stack-send-now, stack-retry and native-session-reopen tests | Keep relevant observable contracts; they protect receipt matching, refusals, no replay, bounded real native startup and session identity. |

Recommended local acceptance after implementation: one actual installed
stdio→runtime→owner prompt/queue/cancel exercise using the pinned native launch
with a bounded no-provider protocol fixture; real local package startup/preflight
without sending a paid prompt; malformed/unattested launch proves no send and
useful failure feedback; headless entrypoint routing exercised through the same
owner. Source/mock tests alone are not installed acceptance. No CI waiting is
required by the user's standing instruction.

## 5. Remaining L0 ownership map

Open PRs verified during audit: parent L0B #229; S13 #232; S10 #234; L0A #235;
S9 #236; S12 #237. A PR's changed-file list includes inherited commits and is not
itself an ownership claim. Map by round2 surface scope, then coordinate exact
crossing files. No new work was assigned by this audit. Lexical inventory is in
`marker-inventory.json`; it deliberately includes case-insensitive comment hits.

### Remaining mechanisms outside the narrowly implemented L0A loader files

| Files / source evidence | Recommended owner and real action |
|---|---|
| `input_disposition.py:82,201,221`, `input_attempt.py:102`, `goal_management.py:66-108` | Parent L0B plus L0A caller agreement: `DeliveryCursor.legacy_through` is actual persisted migration state, used to partition notices and dismiss historical inputs. Delete migration-era cursor/category path, update Goals/ACP/runtime/Toad callers; use existing `InputAttempt` identity/state and owner-bound current queue facts. Classify/reset runtime stores; retain UNKNOWN/no-replay semantics. Not a rename. |
| `goal_actions.py:467` | L0 remaining, parent owns until explicit handoff: Retry creates a missing private goal generation to adopt a registry-only goal. Delete on-retry adoption; establish current grants at the authorized state cutover and fail explicit invalid current-state retry. Preserve explicit retry, not implicit grant recreation. This file also appears in S13's inherited diff: coordinate rather than overwrite. |
| `goal_states.py:162` | L0 remaining: nullable `BlockedGoal.block_reason` explicitly accepts old rows. Current blocked state should require its reason. Saved goal/history closure must go with existing L0A goal-format work, not an isolated annotation change. |
| `goal_waits.py:25,195`, `relationships.py:339` | L0 remaining: optional owner/turn binding accommodates old waits and unbound goals. Remove old-row acceptance after runtime-state classification/reset. Preserve genuinely waiting-on-idle peers, new live turns and stale incarnation rejection via existing `GoalWait`, `GoalWaitTarget`, `ThreadIncarnation`/turn identity. Do not blindly replace every Optional. |
| `candidate_maintenance.py:25,69`, `private_nk_entrypoint.py:59`, `acp.py:574-610` | Parent L0B: finish current-route-only setup and remove old/public metadata alternatives after ordinary current empty-root behavior is defined. Keep candidate-index work optional and off publication's critical path; a missing/corrupt optional index must not undo a committed send. Keep root/package and unbound-prompt rejection. |
| `bus_publication.py:69`, `messaging.py:74,177` | Parent L0B: retain current immutable creation identity, collision rejection and original-root write fencing while deployed old processes are possible; retire cutover-only fence machinery with cutover. Do not change durable identity bytes just to remove a `v1` spelling. Coordinate all dependent typed-table references if identity changes. |
| `messages.py:49` | Parent L0B with D22 wire/history closure: `MessageWireCodec` explicitly admits non-finite timestamps for retained history/export. This is behavior, not just a comment. Inventory/handle such durable records in the one-shot history rewrite; do not discard owner history or rename the comment while retaining an old reader. Preserve export's truthful handling of history not otherwise rewritten. |
| `history_views.py:95`, `threads.py:116,140`, `tools.py:429,796` | L0A scope in plan, but the current narrow #235 receipt does not cover all historical targets. Parent should assign the remaining exact files: missing notification store is absent evidence, Pi session headers are externally owned, current zero/unknown creation identity handling needs registry closure. Goal-history baselines are durable observations, not permission to delete history. Tool collaborations' explicit relationship declarations are independent editable declarations, not automatically dead because their comment says Legacy. |
| `owned_turn.py:629`, `locked_store.py:61`, `routing.py:83`, `field_codec.py:164`, `native_transcript.py:40`, `wire_watch.py:4,72`, `recovery_gateway_client.py:107`, `nk_foreground.py:6` | Parent L0 remaining terminology/contract review: several are negative guarantees, mergeable routes, JSON encodability, reverse seeking, or OS notification plus periodic reconciliation. Preserve real behavior; make wording exact. For LockedStore remove permission for normalizing old shapes rather than introducing a converter. No blanket replacement across identifiers/data. |
| `passive_channel_awareness.py:138` | Parent L0 remaining: initialization at current high water prevents invented historical awareness. Preserve current fresh-owner semantics; delete only old-format accommodation if found, not the no-retroactive-awareness rule. |

### Existing active surface owners — route these there, don't duplicate

| Surface | Remaining marker/file scope |
|---|---|
| **S13 #232** | `backend.py` raw-text deletion intersects attached child work; `recovery_gateway.py:310` deliberately rejects oversized socket paths rather than silently choosing `/tmp`. Keep OS behavior, remove misleading negative wording. |
| **S9 #236** | `manual_compaction.py`, `manual_compaction_bridge.py`, `native_session_reopen.py`, `fresh_private_session.py`, `owner_compaction_process.py`, `owner_compaction_settings.py`, `owner_compaction_commit.py`. Manual and owner compaction are separate operations under D21; do not delete one merely for its label. Remove duplicate unjournaled writer access to the canonical root. Preserve Pi session/version/settings validation and explicit failure behavior. |
| **S10 #234** | `native_pi.py`, `selected_pi_summary_rpc.py`; preserve native ACK/context proof and no automatic replay. Native launch owner extension intersects this scope. |
| **S12 #237, including plan-declared tables not yet in its diff** | `cohort_schema.py`, `coordination_cohort.py`, `coordinated_runtime.py`, `native_source_cursor.py`, `bus_page_index.py`, **`transcript_routes.py`**. Existing singleton-claim rejection prevents old authority adoption; retain fail-closed/current cohort identity while deleting old schema acceptance. Optional awareness indexes are rebuildable; missing/corrupt derived evidence must not fabricate awareness. |

**Concrete S12 omission candidate:** `TranscriptRoutes._ensure_database:96` still
checks `transcript_routes.json`; `_import_saved_routes:129-155` examines old columns,
imports JSON, accepts the old `source='legacy'` shape, drops that column and stores
`routes_imported`. Remove the initializer's import path, migration source property,
source-column/legacy_revision branches and migration metadata/table when unused.
S12 owns the replacement typed table. Classify annotations before resetting:
they include owner-authored display/routing, so do not assume all are recoverable
from Pi logs. If preservation is needed, use the authorized one-shot tool outside
`src`, then delete it. Delete `test_legacy_json_routes_migrate_without_losing_new_records`
and `test_one_way_import_preserves_indexed_routes_and_retires_source_column` with
the old reader, retaining current route persistence and page behavior tests.

## 6. Genuine external boundaries and guard limits

* Pi RPC JSON/event kinds, input IDs, Pi settings/session files, ACP's standard
  envelope, OS signals/process identity and SQLite syntax remain exact external
  contracts. A launch label does not authorize bypassing proof.
* Our runtime socket actions, our `agentComms` queue metadata, backend selection
  rules, internal JSON/SQLite schema versions and old command implementations are
  ours. Migrate their callers in the same closure; no permanent converters.
* `manual_compaction.py` references the actual dependency path
  `node_modules/openai/internal/shims.mjs`. Renaming that path to satisfy a lexical
  grep would break the external package contract. R0's guard must implement the
  stated identifier/comment rule and distinguish external string literals. This
  is not a per-file exception or an excuse to whitelist internal old formats.
* `native_transcript` reverse reads and `routing` mergeable channel batches are
  not old protocol support. Likewise `recovery_gateway` refusal to choose another
  socket location, startup evidence failures and `wire_watch` periodic checking
  must not be deleted as accidental lexical matches.

## 7. NRA evidence and confidence

Installed NRA receipt remains
`~/.local/state/nra-r1-acceptance-20260928/provenance.json`: complete 81/81 scan of
Comms `bf68bbbf`, not this dirty parent integration. Its raw leads identify
`backend._pi_mcp_live_receipt` record shapes and `_tool_title` key reads. Those
support later boundary ownership work; they do not prove the text backend unused
or authorize deleting arbitrary external fields. Production selection above was
established through source/config/process evidence, not a detector label.

Audit change counts: production 0 deleted / 0 added; tests 0 deleted / 0 added.
Implementation/deployment and final behavioral acceptance remain with the parent.
